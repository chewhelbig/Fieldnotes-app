import os
from datetime import datetime, timezone

import psycopg2
import stripe
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI()

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
PRICE_ID = os.environ.get("STRIPE_PRICE_ID_MONTHLY", "")

APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://fieldnotes-app-1.onrender.com").rstrip("/")

def get_conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is missing")
    return psycopg2.connect(url, sslmode="require")



def upsert_user(email: str):
    """
    Safe upsert:
    - Ensures the user row exists
    - Does NOT grant trial credits
    - Leaves credits_remaining unchanged if user already exists
    """
    email = (email or "").strip().lower()
    if not email:
        return None

    conn = get_conn()
    cur = conn.cursor()

    # Insert user if missing with 0 credits (safe default)
    cur.execute(
        """
        INSERT INTO users (email, plan, credits_remaining, monthly_allowance, last_reset, subscription_status)
        VALUES (%s, 'free', 0, 0, CURRENT_DATE, 'free')
        ON CONFLICT (email) DO NOTHING
        """,
        (email,),
    )
    conn.commit()
    cur.close()
    conn.close()
    return email


def grant_pro_monthly_credits(email: str):
    """
    Set the user to Pro and grant 100 credits/month (and set current credits to 100).
    Safe to call multiple times.
    """
    email = (email or "").strip().lower()
    if not email:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE users
        SET
          plan = 'pro',
          monthly_allowance = 100,
          credits_remaining = 100,
          last_reset = CURRENT_DATE
        WHERE email = %s
        """,
        (email,),
    )
    conn.commit()
    cur.close()
    conn.close()



def update_user_subscription(email: str, sub_obj: dict):
    email = (email or "").strip().lower()
    if not email:
        return
    status = sub_obj.get("status")
    customer_id = sub_obj.get("customer")
    sub_id = sub_obj.get("id")
    period_end_ts = sub_obj.get("current_period_end")
    period_end = None
    if period_end_ts:
        period_end = datetime.fromtimestamp(period_end_ts, tz=timezone.utc)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE users
        SET stripe_customer_id=%s,
            stripe_subscription_id=%s,
            subscription_status=%s,
            current_period_end=%s
        WHERE email=%s
    """, (customer_id, sub_id, status, period_end, email))
    conn.commit()
    cur.close()
    conn.close()

def add_credits(email: str, amount: int):
    if amount <= 0:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE users
        SET credits_remaining = COALESCE(credits_remaining, 0) + %s
        WHERE email=%s
    """, (amount, email))
    conn.commit()
    cur.close()
    conn.close()

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/create-checkout-session")
async def create_checkout_session(payload: dict):
    email = (payload.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="email required")


    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=email,
        line_items=[{"price": PRICE_ID, "quantity": 1}],
        # Redirect back to your Streamlit app:
        success_url=f"{APP_BASE_URL}/?success=1&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{APP_BASE_URL}/?canceled=1",
        metadata={"email": email},
        subscription_data={"metadata": {"email": email}},
    )
    return {"url": session.url}

@app.post("/create-portal-session")
async def create_portal_session(payload: dict):
    email = (payload.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="email required")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT stripe_customer_id FROM users WHERE email=%s", (email,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row or not row[0]:
        raise HTTPException(status_code=400, detail="No Stripe customer found. Subscribe first.")
    customer_id = row[0]

    portal = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=f"{APP_BASE_URL}/",
    )
    return {"url": portal.url}

@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    if not sig:
        raise HTTPException(status_code=400, detail="Missing Stripe-Signature header")

    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook signature verification failed: {e}")

    etype = event["type"]
    obj = event["data"]["object"]
    
    if etype == "checkout.session.completed":
        session = obj
    
        email = (
            (session.get("customer_details") or {}).get("email")
            or session.get("customer_email")
            or ""
        ).strip().lower()
    
        if email:
            upsert_user(email)
    
            # ✅ ADD THIS BLOCK HERE
            customer_id = session.get("customer")
            if customer_id:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute(
                    "UPDATE users SET stripe_customer_id=%s WHERE email=%s",
                    (customer_id, email),
                )
                conn.commit()
                cur.close()
                conn.close()
    
            grant_pro_monthly_credits(email)
    
        return JSONResponse({"received": True})


    if etype.startswith("customer.subscription."):
        email = ((obj.get("metadata") or {}).get("email") or "").strip().lower()
        if email:
            update_user_subscription(email, obj)
    
            status = (obj.get("status") or "").lower()
            # ✅ When subscription becomes active/trialing, ensure Pro credits are granted
            if status in ("active", "trialing"):
                # Only grant credits if user has no monthly allowance yet
                conn = get_conn()
                cur = conn.cursor()
                cur.execute(
                    "SELECT monthly_allowance FROM users WHERE email=%s",
                    (email,),
                )
                row = cur.fetchone()
                cur.close()
                conn.close()
            
                if not row or not row[0]:
                    grant_pro_monthly_credits(email)

    
        return JSONResponse({"received": True})
    

    if etype in ("invoice.payment_succeeded", "invoice.paid"):
        sub_details = obj.get("subscription_details") or {}
        email = ((sub_details.get("metadata") or {}).get("email") or "").strip().lower()
        if not email:
            email = ((obj.get("metadata") or {}).get("email") or "").strip().lower()
    
        billing_reason = obj.get("billing_reason")
    
        # IMPORTANT: only grant credits on monthly renewals
        if email and billing_reason == "subscription_cycle":
            grant_pro_monthly_credits(email)  # sets credits_remaining=100 cleanly

    
        return JSONResponse({"received": True})

    return JSONResponse({"received": True})
