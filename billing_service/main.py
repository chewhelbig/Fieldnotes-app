import os
from datetime import datetime, timezone

import psycopg2
import stripe
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
import logging
from psycopg2 import errors as pg_errors




stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
PRICE_ID = os.environ.get("STRIPE_PRICE_ID_MONTHLY", "")

APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://fieldnotes-app-1.onrender.com").rstrip("/")

def get_conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is missing")
    return psycopg2.connect(url, sslmode="require")

logger = logging.getLogger("billing_service")
logging.basicConfig(level=logging.INFO)


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


def ensure_users_schema_minimal():
    conn = get_conn()
    cur = conn.cursor()
    try:
        # Ensure base table exists
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
              email TEXT PRIMARY KEY,
              plan TEXT DEFAULT 'free',
              credits_remaining INT DEFAULT 0,
              monthly_allowance INT DEFAULT 0,
              last_reset DATE,
              stripe_customer_id TEXT,
              stripe_subscription_id TEXT,
              subscription_status TEXT,
              current_period_end TIMESTAMPTZ,
              created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        # Ensure the column your email logic depends on exists
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_credits_granted_at TIMESTAMPTZ;")
        conn.commit()
    finally:
        cur.close()
        conn.close()

def ensure_billing_schema():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_credits_granted_at TIMESTAMPTZ;")
    conn.commit()
    cur.close()
    conn.close()

def ensure_billing_pg_schema():
    conn = get_pg_conn()
    if conn is None:
        return
    try:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS webhook_log (
              id SERIAL PRIMARY KEY,
              event_id TEXT UNIQUE,
              event_type TEXT,
              email TEXT,
              processed BOOLEAN DEFAULT FALSE,
              error TEXT,
              created_at TIMESTAMPTZ DEFAULT NOW(),
              processed_at TIMESTAMPTZ
            );
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_webhook_log_processed_created
            ON webhook_log (processed, created_at);
        """)

        conn.commit()
        cur.close()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def pg_webhook_log_insert(event_id: str, event_type: str, email: str | None):
    conn = get_pg_conn()
    if conn is None:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO webhook_log (event_id, event_type, email, processed)
            VALUES (%s, %s, %s, FALSE)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (event_id, event_type, email),
        )
        conn.commit()
        cur.close()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def pg_webhook_log_mark_processed(event_id: str):
    conn = get_pg_conn()
    if conn is None:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE webhook_log
            SET processed=TRUE, processed_at=NOW(), error=NULL
            WHERE event_id=%s
            """,
            (event_id,),
        )
        conn.commit()
        cur.close()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def pg_webhook_log_mark_error(event_id: str, err: str):
    conn = get_pg_conn()
    if conn is None:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE webhook_log
            SET error=%s
            WHERE event_id=%s
            """,
            (err[:1000], event_id),
        )
        conn.commit()
        cur.close()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


# --- Optional SendGrid dependency (do not crash app if missing) ---
SENDGRID_AVAILABLE = True
try:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail
except Exception:
    SENDGRID_AVAILABLE = False
    SendGridAPIClient = None
    Mail = None


# === send onboarding email ======
def send_onboarding_email(to_email: str, subject: str, text: str, html: str | None = None):
    # Graceful fallback: do nothing if SendGrid isn't available (but log once)
    if not SENDGRID_AVAILABLE:
        logger.warning("SendGrid not installed; skipping email to %s", to_email)
        return

    api_key = (os.environ.get("SENDGRID_API_KEY") or "").strip()
    from_email = (os.environ.get("SENDGRID_FROM_EMAIL") or "nicole@psychotherapist.sg").strip()

    # Graceful fallback: do nothing if not configured (but log once)
    if not api_key or not from_email:
        logger.warning("SendGrid not configured (missing key/from); skipping email to %s", to_email)
        return

    msg = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject=subject,
        plain_text_content=text,
        html_content=html or None,
    )
    SendGridAPIClient(api_key).send(msg)

def email_subscription_started_body(trial_user: bool, portal_link: str | None):

    landing = "https://psychotherapist.sg/fieldnotes"
    app_url = "https://fieldnotes.psychotherapist.sg"

    manage_text = ""
    if portal_link:
        manage_text = f"""

Manage your subscription here:
{portal_link}
"""

    if trial_user:
        subject = "You’re subscribed — FieldNotes is now fully unlocked ✅"
        text = f"""Dear Colleague,

Welcome officially — your subscription is active.

You now have 100 credits/month, and you can use FieldNotes as your ongoing session companion:
- clinical notes (SOAP-informed)
- supervision reflection prompts
- Contact Cycle structure (when useful)

Quick guide + SOAP/Contact Cycle explainer:
{landing}

Open the app:
{app_url}
{manage_text}

Warmly,
Nicole Chew-Helbig
"""
    else:
        subject = "Welcome to FieldNotes — let’s get you set up ✅"
        text = f"""Dear Colleague,

Welcome to FieldNotes — your subscription is active.

This isn’t just a session-writing tool. It’s designed as a professional companion for better clinical thinking and continuity.

Quick guide + SOAP/Contact Cycle explainer:
{landing}

Open the app:
{app_url}
{manage_text}

Warmly,
Nicole Chew-Helbig
"""

    return subject, text


# === grant monthly credits ==========
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

app = FastAPI()

@app.on_event("startup")
def on_startup():
    ensure_billing_pg_schema()

@app.get("/health")
def health():
    # Email availability checks (no email sent)
    sendgrid_api_key_set = bool((os.environ.get("SENDGRID_API_KEY") or "").strip())
    sendgrid_from_set = bool((os.environ.get("SENDGRID_FROM_EMAIL") or "").strip())

    email_ready = bool(SENDGRID_AVAILABLE and sendgrid_api_key_set and sendgrid_from_set)

    # Optional DB check (quick)
    db_ok = False
    db_error = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1;")
        cur.fetchone()
        cur.close()
        conn.close()
        db_ok = True
    except Exception as e:
        db_error = str(e)

    payload = {
        "ok": True,
        "service": "billing_service",
        "email": {
            "sendgrid_installed": bool(SENDGRID_AVAILABLE),
            "sendgrid_api_key_set": sendgrid_api_key_set,
            "sendgrid_from_set": sendgrid_from_set,
            "ready": email_ready,
        },
        "db": {
            "ok": db_ok,
            "error": db_error,
        },
    }
    return JSONResponse(payload)




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

# ======create billing portal link ===========
def create_billing_portal_link(customer_id: str) -> str | None:
    if not customer_id:
        return None

    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url="https://fieldnotes.psychotherapist.sg",
        )
        return session.url
    except Exception:
        return None

@app.get("/billing-portal-link")
def get_billing_portal_link(email: str):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT stripe_customer_id FROM users WHERE email = %s",
            (email,),
        )
        row = cur.fetchone()
        cur.close()

        if not row or not row[0]:
            return {"url": None}

        portal_url = create_billing_portal_link(row[0])
        return {"url": portal_url}

    finally:
        conn.close()

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

    event_id = event["id"]
    email = None

    try:
        # Log receipt (idempotent)
        pg_webhook_log_insert(event_id, etype, email)

        # ------------------------------------------------------------
        # 1) Subscription started (Checkout)
        # ------------------------------------------------------------
        if etype == "checkout.session.completed":
            session = obj

            email = (
                (session.get("customer_details") or {}).get("email")
                or session.get("customer_email")
                or ""
            ).strip().lower()

            if email:
                upsert_user(email)

                customer_id = session.get("customer")
                sub_id = session.get("subscription")

                if customer_id or sub_id:
                    conn = None
                    cur = None
                    try:
                        conn = get_conn()
                        cur = conn.cursor()

                        if customer_id and sub_id:
                            cur.execute(
                                "UPDATE users SET stripe_customer_id=%s, stripe_subscription_id=%s WHERE email=%s",
                                (customer_id, sub_id, email),
                            )
                        elif customer_id:
                            cur.execute(
                                "UPDATE users SET stripe_customer_id=%s WHERE email=%s",
                                (customer_id, email),
                            )
                        else:
                            cur.execute(
                                "UPDATE users SET stripe_subscription_id=%s WHERE email=%s",
                                (sub_id, email),
                            )

                        conn.commit()
                    finally:
                        if cur:
                            cur.close()
                        if conn:
                            conn.close()

                grant_pro_monthly_credits(email)

        # ------------------------------------------------------------
        # 2) Subscription status updates (NO credit reset)
        # ------------------------------------------------------------
        elif etype.startswith("customer.subscription."):
            email = ((obj.get("metadata") or {}).get("email") or "").strip().lower()
            if email:
                update_user_subscription(email, obj)

        # ------------------------------------------------------------
        # 3) Monthly renewal (ONLY subscription_cycle)
        # ------------------------------------------------------------
        elif etype == "invoice.payment_succeeded":
            invoice = obj
            billing_reason = invoice.get("billing_reason")

            sub_details = invoice.get("subscription_details") or {}
            email = (
                (sub_details.get("metadata") or {}).get("email")
                or (invoice.get("metadata") or {}).get("email")
                or ""
            ).strip().lower()

            if email and billing_reason == "subscription_cycle":
                grant_pro_monthly_credits(email)

        # ---- other etype blocks go here ----

        pg_webhook_log_mark_processed(event_id)
        return JSONResponse({"received": True})

    except Exception as e:
        pg_webhook_log_mark_error(event_id, str(e))
        raise


    return JSONResponse({"received": True})

    
    return JSONResponse({"received": True})
