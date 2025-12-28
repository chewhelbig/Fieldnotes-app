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
    return psycopg2.connect(os.environ["DATABASE_URL"])

def upsert_user(email: str) -> str:
    email = (email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="email required")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (email)
        VALUES (%s)
        ON CONFLICT (email) DO NOTHING
    """, (email,))
    conn.commit()
    cur.close()
    conn.close()
    return email

def update_user_subscription(email: str, sub_obj: dict):
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
    email = upsert_user(payload.get("email"))

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

    if etype.startswith("customer.subscription."):
        email = ((obj.get("metadata") or {}).get("email") or "").strip().lower()
        if email:
            update_user_subscription(email, obj)

    if etype in ("invoice.payment_succeeded", "invoice.paid"):
        # Email is stored in metadata we set on the subscription/session
        sub_details = obj.get("subscription_details") or {}
        email = ((sub_details.get("metadata") or {}).get("email") or "").strip().lower()
        if not email:
            email = ((obj.get("metadata") or {}).get("email") or "").strip().lower()

        billing_reason = obj.get("billing_reason")
        if email and billing_reason in ("subscription_create", "subscription_cycle"):
            add_credits(email, 30)  # monthly credits

    return JSONResponse({"received": True})
