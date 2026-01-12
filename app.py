import os
import io
import re
from datetime import datetime
import streamlit as st
from openai import OpenAI
from fpdf import FPDF
import streamlit.components.v1 as components
import requests
from datetime import date
import psycopg2
import hashlib
import time
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail



# ====== database ===========
def get_pg_conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    # psycopg2 accepts the URL string directly
    return psycopg2.connect(url, sslmode="require")


def ensure_pg_schema():
    conn = get_pg_conn()
    if conn is None:
        return  # no DATABASE_URL set, skip
    try:
        cur = conn.cursor()
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
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_code_hash TEXT;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_expires_at TIMESTAMPTZ;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_attempts INT NOT NULL DEFAULT 0;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_credits_granted_at TIMESTAMPTZ;")

        # Subscriber PIN (safe to run repeatedly)
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS app_pin TEXT;")

        conn.commit()
        cur.close()
    finally:
        conn.close()



TRIAL_INVITE_CODE = os.environ.get("TRIAL_INVITE_CODE", "").strip()

DEFAULT_MONTHLY_ALLOWANCE = 100
TRIAL_CREDITS = 7  # put this near your other constants

def pg_get_or_create_user(email: str):
    """
    Returns: (row, created)
    Row shape: (email, plan, credits_remaining, monthly_allowance, last_reset, subscription_status)
    """
    email = (email or "").strip().lower()
    if not email:
        return None, False

    conn = get_pg_conn()
    if conn is None:
        return None, False

    try:
        cur = conn.cursor()

        # 1) Try fetch
        cur.execute(
            """
            SELECT email, plan, credits_remaining, monthly_allowance, last_reset, subscription_status
            FROM users
            WHERE email = %s
            """,
            (email,),
        )
        row = cur.fetchone()
        if row:
            cur.close()
            return row, False

        # 2) Not found -> insert with safe default credits
        starting_credits = 0
        plan = "free"

        cur.execute(
            """
            INSERT INTO users (
                email, plan, credits_remaining, monthly_allowance, last_reset, subscription_status
            )
            VALUES (%s, %s, %s, 0, CURRENT_DATE, 'none')
            ON CONFLICT (email) DO NOTHING
            RETURNING email, plan, credits_remaining, monthly_allowance, last_reset, subscription_status
            """,
            (email, plan, starting_credits),
        )
        inserted = cur.fetchone()
        conn.commit()

        if inserted:
            cur.close()
            return inserted, True  # truly created

        # If we didn't insert (conflict), fetch and return created=False
        cur.execute(
            """
            SELECT email, plan, credits_remaining, monthly_allowance, last_reset, subscription_status
            FROM users
            WHERE email = %s
            """,
            (email,),
        )
        row = cur.fetchone()
        cur.close()
        return row, False

    finally:
        conn.close()



def pg_grant_trial_credits_once(email: str) -> bool:
    email = (email or "").strip().lower()
    if not email:
        return False

    # ✅ Ensure the user row exists before updating
    pg_get_or_create_user(email)

    try:
        cur = conn.cursor()

        # Only grant once
        cur.execute(
            """
            UPDATE users
            SET
                credits_remaining = GREATEST(credits_remaining, %s),
                trial_credits_granted_at = NOW()
            WHERE email = %s
              AND trial_credits_granted_at IS NULL
            """,
            (TRIAL_CREDITS, email),
        )

        changed = cur.rowcount > 0
        conn.commit()
        cur.close()
        return changed

    finally:
        conn.close()



def pg_maybe_reset_monthly(email: str):
    conn = get_pg_conn()
    if conn is None:
        return
    cur = conn.cursor()

    cur.execute(
        "SELECT last_reset, monthly_allowance FROM users WHERE email=%s",
        (email,)
    )
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return

    last_reset, allowance = row
    if not allowance:
        cur.close()
        conn.close()
        return

    today = date.today()

    # Reset when calendar month changes
    if (
        last_reset is None
        or last_reset.year != today.year
        or last_reset.month != today.month
    ):
        cur.execute(
            """
            UPDATE users
            SET credits_remaining=%s,
                last_reset=%s
            WHERE email=%s
            """,
            (allowance, today, email),
        )
        conn.commit()

    cur.close()
    conn.close()

def pg_reset_app_pin(email: str) -> bool:
    email = (email or "").strip().lower()
    if not email:
        return False

    conn = get_pg_conn()
    if conn is None:
        return False

    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET app_pin = NULL WHERE email = %s",
            (email,),
        )
        conn.commit()
        changed = cur.rowcount > 0
        cur.close()
        return changed
    finally:
        conn.close()



# --- Admin access (Stage 2) ---
def get_admin_emails() -> set[str]:
    """
    Comma-separated list in Render env var FIELDNOTES_ADMIN_EMAILS
    e.g. "nikhelbig@gmail.com"
    """
    raw = os.environ.get("FIELDNOTES_ADMIN_EMAILS", "").strip()
    if not raw:
        return set()
    return {e.strip().lower() for e in raw.split(",") if e.strip()}

ADMIN_EMAILS = get_admin_emails()

def is_admin(email: str) -> bool:
    return bool(email) and email.strip().lower() in ADMIN_EMAILS

def pg_get_user(email: str):
    email = (email or "").strip().lower()
    if not email:
        return None

    conn = get_pg_conn()
    if conn is None:
        return None
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT email, plan, credits_remaining, monthly_allowance, last_reset,
                   subscription_status, stripe_customer_id, stripe_subscription_id
            FROM users WHERE email=%s
        """, (email,))
        row = cur.fetchone()
        cur.close()
        return row
    finally:
        conn.close()

def pg_refresh_user(email: str):
    """
    Always fetch the latest user row from Postgres.
    Returns the same shape as pg_get_user:
    (email, plan, credits_remaining, monthly_allowance, last_reset,
     subscription_status, stripe_customer_id, stripe_subscription_id)
    """
    return pg_get_user(email)

# -------------------------
# Subscriber PIN (store HASH, never store the raw PIN)
# -------------------------
def _pin_hash(pin: str) -> str:
    pepper = (os.environ.get("APP_PIN_PEPPER") or "").strip()
    if not pepper:
        raise RuntimeError("Missing APP_PIN_PEPPER (Render env var).")
    data = f"{pepper}:{pin}".encode("utf-8")
    return hashlib.sha256(data).hexdigest()

def pg_get_app_pin_hash(email: str) -> str | None:
    email = (email or "").strip().lower()
    if not email:
        return None
    conn = get_pg_conn()
    if conn is None:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT app_pin FROM users WHERE email=%s", (email,))
        row = cur.fetchone()
        cur.close()
        return (row[0] if row else None)
    finally:
        conn.close()

def pg_set_app_pin_hash(email: str, pin: str) -> None:
    email = (email or "").strip().lower()
    if not email:
        return
    conn = get_pg_conn()
    if conn is None:
        return
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET app_pin=%s WHERE email=%s", (_pin_hash(pin), email))
        conn.commit()
        cur.close()
    finally:
        conn.close()

def pg_check_app_pin(email: str, entered_pin: str) -> bool:
    try:
        stored = pg_get_app_pin_hash(email)
        if not stored:
            return False
        return _pin_hash(entered_pin) == stored
    except Exception:
        # If pepper missing or any error, fail closed
        return False


# --- Prevent double-click spending (UI lock) ---
if "is_generating" not in st.session_state:
    st.session_state["is_generating"] = False


# ===== credit ============

def pg_deduct_credit(email: str) -> bool:
    conn = get_pg_conn()
    if conn is None:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE users
            SET credits_remaining = credits_remaining - 1
            WHERE email=%s AND credits_remaining > 0
            RETURNING credits_remaining
        """, (email,))
        ok = cur.fetchone() is not None
        conn.commit()
        cur.close()
        return ok
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def pg_try_deduct_credits(email: str, amount: int) -> bool:
    """Atomically deduct `amount` credits if available. Returns True if deducted."""
    if amount <= 0:
        return True

    conn = get_pg_conn()
    if conn is None:
        return False  # can't deduct without DB

    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE users
            SET credits_remaining = credits_remaining - %s
            WHERE email = %s AND credits_remaining >= %s
            RETURNING credits_remaining
            """,
            (amount, email, amount),
        )
        ok = cur.fetchone() is not None
        conn.commit()
        cur.close()
        return ok
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def pg_add_credits(email: str, amount: int) -> None:
    if amount <= 0:
        return
    conn = get_pg_conn()
    if conn is None:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE users
            SET credits_remaining = credits_remaining + %s
            WHERE email = %s
        """, (amount, email))
        conn.commit()
        cur.close()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ==========Email Verification ========


OTP_TTL_MINUTES = 15
OTP_MAX_ATTEMPTS = 8

def _hash_code(code: str) -> str:
    # simple sha256 hash; good enough for OTP storage
    return hashlib.sha256(code.encode("utf-8")).hexdigest()

def _utcnow():
    return datetime.now(timezone.utc)

def pg_is_email_verified(email: str) -> bool:
    conn = get_pg_conn()
    if conn is None:
        return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT email_verified_at FROM users WHERE email=%s", (email,))
        row = cur.fetchone()
        cur.close()
        return bool(row and row[0])
    finally:
        conn.close()

def pg_set_verification_code(email: str, code: str, expires_at):
    code_hash = _hash_code(code)
    conn = get_pg_conn()
    if conn is None:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE users
            SET email_verify_code_hash=%s,
                email_verify_expires_at=%s,
                email_verify_attempts=0
            WHERE email=%s
            """,
            (code_hash, expires_at, email),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()

def pg_mark_email_verified(email: str):
    conn = get_pg_conn()
    if conn is None:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE users
            SET email_verified_at=NOW(),
                email_verify_code_hash=NULL,
                email_verify_expires_at=NULL
            WHERE email=%s
            """,
            (email,),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()

def pg_check_verification_code(email: str, code: str) -> tuple[bool, str]:
    """
    Returns (ok, message)
    """
    conn = get_pg_conn()
    if conn is None:
        return False, "Database unavailable."

    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT email_verify_code_hash, email_verify_expires_at, email_verify_attempts, email_verified_at
            FROM users
            WHERE email=%s
            """,
            (email,),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            return False, "No account found."

        code_hash, expires_at, attempts, verified_at = row
        if verified_at:
            cur.close()
            return True, "Already verified."

        if attempts is not None and attempts >= OTP_MAX_ATTEMPTS:
            cur.close()
            return False, "Too many attempts. Please request a new code."

        if not code_hash or not expires_at:
            cur.close()
            return False, "No active code. Please request a new code."

        # handle timezone-naive timestamps from DB safely
        now = _utcnow()
        exp = expires_at
        try:
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
        except Exception:
            pass

        if now > exp:
            cur.close()
            return False, "Code expired. Please request a new one."

        if _hash_code(code.strip()) != code_hash:
            # increment attempts
            cur.execute(
                "UPDATE users SET email_verify_attempts = COALESCE(email_verify_attempts,0) + 1 WHERE email=%s",
                (email,),
            )
            conn.commit()
            cur.close()
            return False, "Incorrect code."

        # correct
        cur.close()
        return True, "Verified."
    finally:
        conn.close()
# ========Send Verification email =======

def send_verification_email(to_email: str, code: str):
    api_key = os.environ.get("SENDGRID_API_KEY", "")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL", "")
    if not api_key or not from_email:
        raise RuntimeError("Missing SENDGRID_API_KEY or FROM_EMAIL env var.")

    subject = "Your FieldNotes verification code"

    body = f"""Hello,

Your FieldNotes verification code is:

{code}

This code is valid for 15 minutes.

Once verified, your free trial credits will be activated and you can start generating notes.

If you didn’t request this, you can safely ignore this email.

Warm regards,
FieldNotes
"""

    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject=subject,
        plain_text_content=body,
    )

    sg = SendGridAPIClient(api_key)
    sg.send(message)

#===========send onboarding email=========
def send_onboarding_email(to_email: str, subject: str, text: str, html: str | None = None):
    """
    Lightweight SendGrid sender for onboarding emails.
    Uses SENDGRID_API_KEY + SENDGRID_FROM_EMAIL env vars.
    """
    api_key = os.environ.get("SENDGRID_API_KEY", "").strip()
    from_email = os.environ.get("SENDGRID_FROM_EMAIL", "nicole@psychotherapist.sg").strip()

    if not api_key:
        raise RuntimeError("SENDGRID_API_KEY missing")
    if not from_email:
        raise RuntimeError("SENDGRID_FROM_EMAIL missing")

    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject=subject,
        plain_text_content=text,
        html_content=html or None,
    )
    sg = SendGridAPIClient(api_key)
    sg.send(message)
#========trial welcome trial verified users=========

def email_trial_verified_body():
    landing = "https://psychotherapist.sg/fieldnotes"
    app_url = "https://fieldnotes.psychotherapist.sg"

    subject = "Welcome to FieldNotes — your 7 free credits are active 🎁"
    text = f"""Dear Colleague,

Welcome to FieldNotes.

This isn’t just a session-writing tool — it’s a professional companion for clearer thinking, better continuity, and better supervision conversations.

Your 7 free trial credits are now active.

Start here (quick guide + concepts like SOAP & Contact Cycle):
{landing}

Open the app:
{app_url}

Warmly,
Nicole Chew-Helbig
"""
    return subject, text





# ============PASSWORD================
def require_app_password_sidebar() -> bool:
    pwd = os.environ.get("APP_ACCESS_PASSWORD")
    if not pwd:
        return True  # no password set → open

    if st.session_state.get("access_ok"):
        st.sidebar.caption("Access: enabled")
        return True

    st.sidebar.markdown("### 🔒 Access")
    entered = st.sidebar.text_input(
        "Access password",
        type="password",
        key="access_password_sidebar"
    )

    if st.sidebar.button("Enter", key="access_enter"):
        if entered == pwd:
            st.session_state["access_ok"] = True
            st.rerun()
        else:
            st.sidebar.error("Incorrect password")

    st.sidebar.info("Enter the access password to enable the app.")
    return False




# ------Get OPEN AI------------
@st.cache_resource
def get_openai_client():
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    return OpenAI(api_key=key)


# =========================
# OpenAI call helpers
# =========================
def call_openai(combined_narrative: str, client_name: str, output_mode: str) -> str:
    client = get_openai_client()
    if client is None:
        raise RuntimeError("Missing OPENAI_API_KEY on server (Render env var).")

    user_prompt = build_prompt(combined_narrative, client_name, output_mode)

    response = client.chat.completions.create(
        model=OPENAI_MODEL_NOTES,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    return response.choices[0].message.content


def call_reflection_engine(narrative: str, ai_output: str, client_name: str, intensity: str) -> str:
    client = get_openai_client()
    if client is None:
        raise RuntimeError("Missing OPENAI_API_KEY on server (Render env var).")

    user_prompt = build_reflection_prompt(
        narrative=narrative,
        ai_output=ai_output,
        client_name=client_name,
        intensity=intensity,
    )

    response = client.chat.completions.create(
        model=OPENAI_MODEL_REFLECTION,
        messages=[
            {"role": "system", "content": REFLECTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.4,
        max_tokens=MAX_TOKENS_REFLECTION,
    )
    return response.choices[0].message.content.strip()



# =========================
# Hosted / download-only mode
# =========================
APP_VERSION = "0.2-hosted-download-only"

OPENAI_MODEL_NOTES = "gpt-4.1-mini"
OPENAI_MODEL_REFLECTION = "gpt-4.1-mini"
MAX_TOKENS_REFLECTION = 2300

BILLING_API_URL = os.getenv(
    "BILLING_API_URL",
    "https://fieldnotes-billing.onrender.com"
)

def start_stripe_checkout(email: str) -> str | None:
    """Create Stripe Checkout Session and return checkout URL."""
    email = (email or "").strip().lower()
    if not email:
        return None

    try:
        r = requests.post(
            f"{BILLING_API_URL}/create-checkout-session",
            json={"email": email},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("url")
    except Exception:
        return None


# ==================
# Credits
#===================

COST_GENERATE_NOTES = 1
REFLECTION_COST = {
    "Basic": 2,
    "Deep": 2,
    "Very deep": 2,
}

# COST_HELDA = 2 -- for later


# =========================
# Text/PDF helpers
# =========================
def normalize_text(s: str) -> str:
    """Normalize common “smart” punctuation to plain ASCII to avoid ascii-encode crashes."""
    if not s:
        return ""
    return (
        s.replace("\u2014", "-")  # em dash —
         .replace("\u2013", "-")  # en dash –
         .replace("\u2018", "'")  # ‘
         .replace("\u2019", "'")  # ’
         .replace("\u201c", '"')  # “
         .replace("\u201d", '"')  # ”
    )

def to_latin1_safe(s: str) -> str:
    """
    Force content to be safe for pyfpdf (latin-1).
    Replaces common unicode punctuation, then drops anything not representable.
    """
    s = normalize_text(s or "")
    s = s.replace("\u00a0", " ")  # non-breaking space
    # Finally: drop any characters pyfpdf can't encode
    return s.encode("latin-1", errors="ignore").decode("latin-1")


def convert_contact_cycle_table_to_prose(text: str) -> str:
    lines = text.split("\n")
    out = []
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        # Detect start of Gestalt Contact Cycle table
        if line.startswith("|") and "Phase of Contact Cycle" in line:
            # Collect table lines
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1

            out.append("GESTALT CONTACT CYCLE ROADMAP\n")

            # Skip header + separator
            for row in table_lines[2:]:
                cells = [c.strip() for c in row.split("|")[1:-1]]
                if len(cells) != 4:
                    continue

                phase, what, indicators, opportunities = cells

                out.append(f"{phase}")
                out.append(f"- What happened in this phase:\n  {what}")
                out.append(f"- Indicators / clues:\n  {indicators}")
                out.append(f"- Opportunities for next time:\n  {opportunities}")
                out.append("")

        else:
            out.append(lines[i])
            i += 1

    return "\n".join(out).strip()



def contact_cycle_table_to_text(table_lines: list[str]) -> str:
    """
    Convert a Markdown contact cycle table into a readable text block
    for the PDF export.
    """
    if not table_lines:
        return ""

    header = table_lines[0]
    header_cells = [c.strip() for c in header.split("|")[1:-1]]
    if len(header_cells) < 4:
        return "\n".join(table_lines)

    result_lines: list[str] = []

    for row in table_lines[1:]:
        row_stripped = row.strip()
        # skip separator-like rows
        if set(row_stripped.replace("|", "").replace("-", "").strip()) == set():
            continue

        cells = [c.strip() for c in row.split("|")[1:-1]]
        if len(cells) < 4:
            continue

        phase, what, indicators, opps = cells[0], cells[1], cells[2], cells[3]

        if phase:
            result_lines.append(phase)
        if what:
            result_lines.append(f"  - What happened: {what}")
        if indicators:
            result_lines.append(f"  - Indicators / clues: {indicators}")
        if opps:
            result_lines.append(f"  - Opportunities for next time: {opps}")
        result_lines.append("")

    return "\n".join(result_lines).strip()


def create_pdf_from_text(content: str) -> bytes:
    """Create a simple, readable PDF from plain text (latin-1 safe for FPDF)."""
    safe_content = to_latin1_safe(content or "")

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Arial", size=11)

    page_width = pdf.w - 2 * pdf.l_margin

    for line in safe_content.split("\n"):
        line = line.rstrip()

        if not line.strip():
            pdf.ln(4)
            continue

        line = line.replace("**", "")

        if line.startswith("### "):
            pdf.set_font("Arial", "B", 12)
            pdf.multi_cell(page_width, 7, line.replace("### ", ""))
            pdf.set_font("Arial", "", 11)
            pdf.ln(1)
            continue

        if line.startswith("## "):
            pdf.set_font("Arial", "B", 13)
            pdf.multi_cell(page_width, 8, line.replace("## ", ""))
            pdf.set_font("Arial", "", 11)
            pdf.ln(2)
            continue

        if line.startswith("# "):
            pdf.set_font("Arial", "B", 14)
            pdf.multi_cell(page_width, 9, line.replace("# ", ""))
            pdf.set_font("Arial", "", 11)
            pdf.ln(3)
            continue

        pdf.multi_cell(page_width, 6, line)

    pdf_out = pdf.output(dest="S")

    if isinstance(pdf_out, str):
        pdf_out = pdf_out.encode("latin-1", errors="ignore")
    if isinstance(pdf_out, bytearray):
        pdf_out = bytes(pdf_out)

    return pdf_out






def safe_download_name(label: str) -> str:
    """Make a safe filename component (no directories)."""
    label = (label or "").strip()
    if not label:
        return "client"

    # Keep alnum, space, underscore, dash only
    cleaned = "".join(c for c in label if c.isalnum() or c in " _-")
    cleaned = cleaned.strip().replace(" ", "_")
    return cleaned or "client"


# =========================
# Prompts (unchanged)
# =========================
SYSTEM_PROMPT = """
You are a clinical note assistant for psychotherapists who work in a relational, Gestalt-oriented way.

The therapist will give you a raw, informal narrative of a therapy session. It may include:
- paraphrases of what the client said
- direct quotes
- what the therapist felt, thought, assumed
- what intervention / experiment was done
- what seemed to happen in the outcome

You MUST:
- Not invent facts or events that are not in the narrative.
- Not add diagnoses or labels unless explicitly mentioned.
- Keep ambiguity and uncertainty when present.
- Use gentle, tentative language in your interpretations.
- Avoid "why" questions and yes/no questions completely.

You will produce up to FIVE sections:

1) CLEAN NARRATIVE
- Rewrite the therapist's narrative in clear, professional English.
- Keep the meaning, but make it more coherent and readable.
- Include both client process and therapist process if present.

2) GESTALT-STYLE SOAP NOTE
Use these SPECIAL definitions:

S (Subjective):
- What the client reported, said, felt, wanted (paraphrased).
- Include what the therapist felt, sensed, assumed or reflected on.
- Use the therapist's language where possible but clean up English.

O (Objective):
- Concrete, observable facts and events only.
- Examples: arrived late/early, posture, tears, tone, where they sat, actions,
  notable silences, direct short quotes, interventions/experiments done and
  what happened in response.
- No interpretations here, only what a camera or microphone could record.

A (Analysis):
- Your process-oriented understanding/hypothesis.
- Gestalt-flavoured: figure/ground, contact/withdrawal, interruption of contact,
  field conditions, creative adjustments, shame, confluence, retroflection, etc.
- Use tentative language: "It appears that...", "One possible understanding is...",
  "It may be that...".
- Focus on relational process, not pathology.
- When you see past 'A (Analysis)' sections (included as background context),
  treat them only as contextual background.
  Do not copy or repeat them verbatim.
  If the current session appears to contradict earlier views, privilege the present
  session material and describe the shift tentatively (e.g., "It may be that something
  new is emerging in the client's process compared to previous sessions...").

P (Plan / Points to consider):
- Things for the therapist to consider in future sessions.
- Focus on what/how questions to deepen awareness or contact.
- May include possible experiments or directions, framed as suggestions, NOT orders.
- Do NOT use any "why" questions or yes/no questions.

3) SUPERVISOR-STYLE QUESTIONS FOR THE THERAPIST
- 5–8 open, process-oriented questions that a good Gestalt supervisor might ask.
- Use only "what" and "how" questions.
- No "why" and no yes/no questions.
- Focus on therapist awareness, relational field, and embodied experience.

4) GESTALT CONTACT CYCLE ROADMAP (TABLE)
- Based ONLY on the narrative, map key parts of the session onto the contact cycle.
- Use this structure, in Markdown table format:

| Phase of Contact Cycle | What happened in this phase | Indicators / clues | Opportunities for next time |
|------------------------|----------------------------|--------------------|-----------------------------|
| Pre-contact            | ...                        | ...                | ...                         |
| Fore-contact           | ...                        | ...                | ...                         |
| Mobilisation           | ...                        | ...                | ...                         |
| Action                 | ...                        | ...                | ...                         |
| Contact                | ...                        | ...                | ...                         |
| Final contact          | ...                        | ...                | ...                         |
| Post-contact           | ...                        | ...                | ...                         |

- If some phases are unclear from the narrative, write "unclear from narrative" but still keep the row.

5) UNFINISHED BUSINESS
- Identify possible areas of unfinished business in the session.
- This may include: emotions that started but were cut off, topics that reappeared but were avoided,
  relational ruptures that were not worked through, things the therapist felt but did not say,
  places where the client withdrew, etc.
- Present as short bullet points, each one grounded in something from the narrative.
- Do NOT dramatise; keep it grounded and professional.

GENERAL RULES:
- Do not mention these instructions in your output.
- Do not apologise or add meta-commentary.
- Write everything as if you are returning notes directly to the therapist.
"""


REFLECTION_SYSTEM_PROMPT = """
You are a supervision and reflection companion for an experienced Gestalt-oriented psychotherapist, 
working with diverse clients in a variety of cultural and relational contexts.

Their work integrates:
- phenomenology and embodied inclusion
- shame as connective tissue in relationships
- cultural and intergenerational patterns, including ancestral consciousness
- field theory as lived atmosphere and co-created movement
- creative experimentation rooted in dialogue rather than technique

Your task is NOT to evaluate or diagnose.
Your role is to help the therapist sense the between, name subtle field movements, and reflect on their own participation.

Use gentle, tentative, phenomenological language:
"It may be that…", "One possibility is…", "Something in the field suggests…".

Keep it concise, embodied, relational, and grounded in Gestalt field thinking.

-----------------------------------
1. THERAPIST PROCESS & COUNTERTRANSFERENCE
-----------------------------------
Help the therapist sense:
- bodily responses, micro-shifts in affect, resonance, irritation, protectiveness, withdrawal, pressure, or confusion
- how they may be recruited into family roles (rescuer, mediator, child, elder, judge, witness, ancestor-representative)
- their own cultural or ancestral echoes activated in the session
- their participation in the co-created reality, not as observer but as part of the field

Make links that are subtle, aesthetic, atmospheric—not dogmatic or interpretive.

-----------------------------------
2. SHAME ARC & DISSOCIATIVE MOVEMENTS
-----------------------------------
Describe the shame arc as field movement, not as pathology.

Consider:
- cycles of exposure → collapse → appeasement → rage → repair
- shame as silence, humour, intellectualising, compliance, withdrawal, attack, distancing
- embodied signs (tension, gaze aversion, breath changes, shrinking or swelling)
- how shame appears between therapist and client, not only in the client

Name possible moments where:
- shame disconnection happened
- shame rescue was expected
- shame was metabolised
- shame was passed across the field like heat or electricity

-----------------------------------
3. FIELD RESONANCE & ATMOSPHERIC SENSING
-----------------------------------
Attend to the wider field:
- pace, rhythm, atmosphere, temperature, heaviness/lightness
- interruptions, accelerations, voids, lingering silences
- triangulations in the field (partner, family member, ancestor, institution)
- the path of least resistance and the path of most aliveness

Include aesthetic attunement:
- what the therapist felt, saw, heard, smelled, intuited

-----------------------------------
4. CULTURAL / ANCESTRAL / INTERGENERATIONAL GROUND
-----------------------------------
Gently illuminate:
- expectations, obligations, and relational etiquette in the client’s cultural context
- possible ancestral presence ("who else is in the room")
- inherited family anxieties around shame, belonging, duty, or conflict
- cross-cultural or intra-cultural tensions (e.g. modernity vs tradition, autonomy vs relational duty)
- gendered or role-based expectations shaped by the client’s community
- collective fields the client may be carrying (migration, class mobility, survival narratives, systemic oppression)

Always be humble and tentative:
"An ancestral echo may be present…",
"There may be a family voice speaking through this moment…".

-----------------------------------
5. GESTALT EXPERIMENT PLANNER
-----------------------------------
Offer 3–5 light-touch, field-sensitive experiment directions.

Each experiment should include:
- therapeutic intention (what it might support)
- a simple setup (e.g., sensory awareness, chair work, slowing the breath, turning toward or away, marking boundaries)
- a relational ground: how it supports co-creation, not correction
- a note on risks or sensitivities (cultural, shame-related, trauma-related)

Experiments may include:
- aesthetic slowing
- dialogical pacing
- role-reversal (psychodrama-like)
- embodied polarities
- awareness of emerging pre-contact
- ancestral positioning ("invite the ancestor to sit behind you")
- boundary rituals
- voice-based differentiation
- misattunement repair sequences

-----------------------------------
6. ONE QUESTION BACK TO THE THERAPIST
-----------------------------------
End with 1–3 contemplative questions that help the therapist reflect on:
- their position in the field
- their resonance
- what they may have avoided, softened, intensified, or co-created
- what is asking to emerge next in the relationship

These questions should open space, not close it.

-----------------------------------
GENERAL STYLE:
-----------------------------------
- Phenomenological, embodied, relational.
- No diagnosis.
- No pathologising.
- No expert tone.
- Do not assume any particular culture or country.
- Honour the field.
- Honour the ancestors.
- Honour the therapist’s humanity.
"""


REFLECTION_INTENSITY_INSTRUCTIONS = {
    "Basic": "Keep the reflection brief and gentle. Focus on 1–2 key themes in each section. Avoid too much detail; highlight what stands out most in the field.",
    "Deep": "Offer a fuller reflection with rich but concise descriptions in each section. Name subtle field movements and shame arcs. Keep it readable in a few minutes.",
    "Very deep": "Provide a more extended reflection, staying phenomenological but allowing more nuance and layered hypotheses. Imagine supporting a written supervision process or journal entry.",
}


def build_prompt(narrative: str, client_name: str, output_mode: str) -> str:
    return f"""
Mode: {output_mode}

If mode is "Short":
- Return ONLY sections (1) CLEAN NARRATIVE and (2) a brief GESTALT-STYLE SOAP NOTE.
- Keep SOAP relatively concise.

If mode is "Full":
- Return all sections as described in the system prompt:
  CLEAN NARRATIVE, full GESTALT-STYLE SOAP NOTE, SUPERVISOR-STYLE QUESTIONS,
  GESTALT CONTACT CYCLE ROADMAP table, and UNFINISHED BUSINESS.

Therapist's client name (for context only): {client_name or "Not specified"}

Therapist's raw narrative of the session (informal, possibly messy):

\"\"\"{narrative}\"\"\"

Now produce the output according to the selected mode, with clear Markdown headings for each section you include.
"""


def build_reflection_prompt(
    narrative: str,
    ai_output: str,
    client_name: str,
    intensity: str,
) -> str:
    intensity_instructions = REFLECTION_INTENSITY_INSTRUCTIONS.get(
        intensity,
        REFLECTION_INTENSITY_INSTRUCTIONS["Basic"],
    )

    return f"""
You are helping the therapist reflect on their clinical work.

Client (context only): {client_name or "Not specified"}

----------------
(1) RAW NARRATIVE
----------------
\"\"\"{narrative}\"\"\"

-----------------------
(2) STRUCTURED AI NOTES
-----------------------
\"\"\"{ai_output}\"\"\"

Reflection depth: {intensity}

{intensity_instructions}

Respond in a supervisor-style reflective tone, grounded in Gestalt field theory.
"""
# ======Ensure=============
# =====================
# ======= UI ============
def main():
 
    ensure_pg_schema()


    # ---- Stripe return handling: force refresh after checkout ----
    params = st.query_params
    if params.get("success") == "1":
        st.success("Payment successful — syncing subscription & credits…")
        # Remove the success flag so it doesn't repeat on every rerun
        st.query_params.clear()
        st.rerun()

    if not os.environ.get("DATABASE_URL"):
        st.error("Server misconfiguration: DATABASE_URL is missing (Render env var).")
        st.stop()
    
    # =========================
    # Sidebar
    # =========================
    
    # 1) Sign in (email)
    st.sidebar.markdown("### 👤 Sign in / Sign up")
    
    # Email input FIRST (so user_email exists)
    user_email = st.sidebar.text_input(
        "Email",
        value=st.session_state.get("user_email", ""),
        placeholder="you@clinic.com",
    ).strip().lower()
    
    email_ok = bool(user_email)



    
    #___________________________________________________________   
    # -------------------------
    # Sidebar: Middle portion (varies depending on email/status)
    # -------------------------
    
    subscribe_url = "https://billing.psychotherapist.sg"
    trial_request_url = "https://psychotherapist.sg/fieldnotes-contact-form"
    
    # Defaults
    pg_user = None
    created = False
    credits_remaining = 0
    subscription_status = ""
    existing_user = None
    
   
    # --- Admin access (email allowlist + admin code) ---
    ADMIN_EMAILS = set(
        e.strip().lower()
        for e in os.getenv("FIELDNOTES_ADMIN_EMAILS", "").split(",")
        if e.strip()
    )
    ADMIN_CODE = os.getenv("FIELDNOTES_ADMIN_CODE", "").strip()
    
    # Default: NOT admin
    admin = False
    
    # Only admin emails can even try
    if email_ok and (user_email in ADMIN_EMAILS):
        st.sidebar.markdown("### 🛡️ Admin")
    
        if not ADMIN_CODE:
            st.sidebar.error("Admin access disabled: admin code not configured.")
            st.session_state["admin_ok"] = False
        else:
            entered = st.sidebar.text_input(
                "Admin code",
                type="password",
                key="admin_code_input",
            ).strip()
    
            if entered == ADMIN_CODE:
                st.session_state["admin_ok"] = True
            elif entered:
                st.sidebar.error("Incorrect admin code.")
    
        admin = bool(st.session_state["admin_ok"])
    
    # If email changes away from admin email, force admin off
    if (not email_ok) or (user_email not in ADMIN_EMAILS):
        st.session_state["admin_ok"] = False
        admin = False
    
    
    # ✅ Admin tools ONLY if admin is True
    if admin:
        st.sidebar.markdown("### 🔧 Admin tools")
    
        reset_email = st.sidebar.text_input(
            "Reset subscriber PIN for email",
            key="reset_pin_email",
        ).strip().lower()
    
        if st.sidebar.button("Reset subscriber PIN"):
            if not reset_email:
                st.sidebar.error("Enter an email address first.")
            elif pg_reset_app_pin(reset_email):
                st.sidebar.success("PIN reset. User will be asked to set a new PIN.")
            else:
                st.sidebar.warning("No user found with that email.")

    
    # 0) Opening app (no email)
    if not email_ok:
        st.sidebar.info(
            "Free trial is invite-only. "
            f"[Request free 7 credits]({trial_request_url}). "
            f"You can still subscribe to use immediately."
        )
    
    else:
        # Lookup user
        existing_user = pg_get_user(user_email)
    
        # Helper: lapsed subscriber detection (has Stripe ids but not active)
        has_paid_history = False
        if existing_user:
            # pg_user shape from pg_get_user:
            # (email, plan, credits_remaining, monthly_allowance, last_reset,
            #  subscription_status, stripe_customer_id, stripe_subscription_id)
            stripe_customer_id = existing_user[6] if len(existing_user) > 6 else None
            stripe_subscription_id = existing_user[7] if len(existing_user) > 7 else None
            has_paid_history = bool(stripe_customer_id or stripe_subscription_id)
    
        # -------------------------
        # 1) Unknown email → show welcome + invite code box
        # -------------------------
        if (existing_user is None) and (not admin):
            st.sidebar.info(
                "Thank you for being here.\n\n"
                "To generate notes, please "
                f"subscribe here "
                "or request a "
                f"[**free trial of 7 credits here**]({trial_request_url})."
            )
    
            st.sidebar.markdown("### 🧾 Trial access code")
    
            invite = st.sidebar.text_input(
                "Trial access code",
                type="password",
                key="trial_invite_code",
            ).strip()
    
            # Only when invite code is correct do we create the user record (so verification can happen next)
            if TRIAL_INVITE_CODE and invite == TRIAL_INVITE_CODE:
                pg_user, created = pg_get_or_create_user(user_email, grant_trial=False)
            elif not TRIAL_INVITE_CODE:
                # If you ever remove invite-only mode, allow account creation without code
                pg_user, created = pg_get_or_create_user(user_email, grant_trial=False)
            else:
                pg_user = None
                created = False
    
        else:
            # -------------------------
            # Existing user OR admin → load user directly
            # -------------------------
            if existing_user is not None:
                pg_user = existing_user
                created = False
    
        # -------------------------
        # If we have a user row, unpack latest status
        # -------------------------
        if pg_user:
            # Unpack tuple consistently
            credits_remaining = int(pg_user[2] or 0)
            subscription_status = (pg_user[5] or "").lower()
    
        is_subscribed = subscription_status in ("active", "trialing")
       

        # -------------------------
        # 2) Admin view (always)
        # -------------------------
        if admin:
            st.sidebar.success("Admin view")
    
        # -------------------------
        # 1c / 1d / 1e / 1f messages for known emails
        # -------------------------
        elif existing_user is not None:
            if is_subscribed:
                # 1d subscriber
                st.sidebar.success("Welcome back. Your subscription is live.")
            elif credits_remaining > 0:
                # 1c trial user
                st.sidebar.info(
                    f"You have **{credits_remaining}** credits remaining. "
                    f"**Subscribe** for ongoing use (100 credits/month)."
                )
            elif has_paid_history:
                # 1e lapsed subscriber
                st.sidebar.warning(
                    "Welcome back. Your subscription is not active. "
                    f"**Subscribe** to return."
                )
            else:
                # 1f inactive
                st.sidebar.info(
                    "Welcome.\n\n"
                    "To generate notes, please "
                    f"**subscribe** "
                    "or request a "
                    f"[**free trial of 7 credits here**]({trial_request_url})."
                )
    
        # -------------------------
        # 1b) After trial access code is correct → show verify email box
        #     (only for brand new users created via invite code)
        # -------------------------
        if (not admin) and pg_user and created and (not is_subscribed):
            email_verified = pg_is_email_verified(user_email)
    
            if not email_verified:
                st.sidebar.markdown("### ✅ Verify email to use credits")
                st.sidebar.caption("Verification is required before trial credits are granted.")
    
                if st.sidebar.button("Send verification code", key="send_verify_code"):
                    code = f"{secrets.randbelow(10**6):06d}"
                    expires_at = _utcnow() + timedelta(minutes=OTP_TTL_MINUTES)
                    pg_set_verification_code(user_email, code, expires_at)
    
                    try:
                        send_verification_email(user_email, code)
                        st.sidebar.success("Verification code sent. Check your email.")
                    except Exception as e:
                        st.sidebar.error("Could not send verification email.")
                        st.sidebar.exception(e)
    
                entered_code = st.sidebar.text_input(
                    "Enter 6-digit verification code",
                    key="verify_code_input",
                )
    
                if st.sidebar.button("Verify email", key="btn_verify_email"):
                    ok, msg = pg_check_verification_code(user_email, entered_code)
                    if ok:
                        pg_mark_email_verified(user_email)
    
                        granted = pg_grant_trial_credits_once(user_email, trial_credits=7)
                        if granted:
                            st.sidebar.success("Email verified — 7 free credits added 🎁")
                        else:
                            st.sidebar.info("Email verified. Trial credits were already granted earlier.")
    
                        st.rerun()
                    else:
                        st.sidebar.error(msg)
    
 
   
    # -------------------------
    # -------------------------
    # Access gate (UPDATED)
    # -------------------------
    # 🔒 IMPORTANT:
    # We do NOT show the global access password to brand-new users.
    # Subscribers are protected via their personal PIN (app_pin) instead.
    is_subscribed = (subscription_status or "").lower() in ("active", "trialing")
    
    # Only used to decide if "Generate" actions are allowed later.
    # (UI should still show even if access_ok is False.)
    access_ok = bool(email_ok) and (admin or is_subscribed or (credits_remaining > 0))
    
        
    
    # -------------------------
    # Subscribe / Credits UI (only after email is entered)
    # -------------------------
    if not email_ok:
        st.sidebar.caption("Enter email first to subscribe.")
    else:
        if not is_subscribed:
            # 1) Button FIRST
            if st.sidebar.button("Subscribe USD 29/month", key="btn_subscribe_monthly"):
                try:
                    r = requests.post(
                        f"{BILLING_API_URL}/create-checkout-session",
                        json={"email": user_email},
                        timeout=30,
                    )
                    r.raise_for_status()
                    st.session_state["checkout_url"] = r.json()["url"]
                except Exception as e:
                    st.sidebar.error("Could not start checkout. Please try again.")
                    st.sidebar.exception(e)
    
            checkout_url = st.session_state.get("checkout_url")
            if checkout_url:
                st.sidebar.link_button("Open Stripe Checkout", checkout_url)
    
            # 2) Text AFTER the button
            st.sidebar.warning("Paid plan: USD 29/month")
            st.sidebar.caption("Subscription unlocks 100 credits/month.")
            st.sidebar.caption("Credits reset monthly. Unused credits do not roll over.")
    
        else:
            st.sidebar.success("Subscription: active")
            st.session_state.pop("checkout_url", None)
            # --- Manage subscription link (subscribed users only) ---
            try:
                resp = requests.get(
                    f"{BILLING_API_URL}/billing-portal-link",
                    params={"email": user_email},
                    timeout=5,
                )
                resp.raise_for_status()
                portal_url = resp.json().get("url")
            
                if portal_url:
                    st.sidebar.markdown(f"[Manage subscription]({portal_url})")
            except Exception:
                pass

        # -------------------------
        # Subscriber PIN gate (REQUIRED for subscribers, admin bypass)
        # -------------------------
        
        # Default: if you're a subscriber and not admin, assume NOT OK until proven
        if "subscriber_pin_ok" not in st.session_state:
            st.session_state["subscriber_pin_ok"] = True
        
        if is_subscribed and email_ok and (not admin):
            st.sidebar.markdown("### 🔐 Subscriber PIN (required)")
        
            current_pin_hash = pg_get_app_pin_hash(user_email)
        
            # If subscriber has never set a PIN yet
            if not current_pin_hash:
                st.sidebar.info("Set a PIN to protect your account. You’ll use it each time you sign in.")
        
                new_pin_1 = st.sidebar.text_input("Create PIN (4–8 digits)", type="password", key="set_pin_1").strip()
                new_pin_2 = st.sidebar.text_input("Confirm PIN", type="password", key="set_pin_2").strip()
        
                if st.sidebar.button("Save PIN", key="save_pin"):
                    if (not new_pin_1.isdigit()) or not (4 <= len(new_pin_1) <= 8):
                        st.sidebar.error("PIN must be 4–8 digits.")
                    elif new_pin_1 != new_pin_2:
                        st.sidebar.error("PINs do not match.")
                    else:
                        pg_set_app_pin_hash(user_email, new_pin_1)
                        st.sidebar.success("PIN saved. Please enter it to enable generation.")
                        st.session_state["subscriber_pin_ok"] = False
                        st.rerun()
        
                # Until a PIN exists, generation must be blocked
                st.session_state["subscriber_pin_ok"] = False
        
            # PIN exists → require entry every session to generate
            else:
                entered_pin = st.sidebar.text_input("Enter PIN to enable generation", type="password", key="enter_pin").strip()
                ok = bool(entered_pin) and pg_check_app_pin(user_email, entered_pin)
                st.session_state["subscriber_pin_ok"] = ok
                st.sidebar.caption("Forgot your PIN? Contact support to reset it.")
                if not ok:
                    st.sidebar.caption("Enter your PIN to enable generation.")
    

    
         
    
    st.sidebar.markdown("---")
    
    # Usage + hosted mode info can stay
    st.sidebar.subheader("Usage")
    with st.sidebar.expander("How credits are used"):
        st.sidebar.markdown(
            "- **1 credit = 1 clinical notes generation** (one click on “Generate structured output”).\n"
            "- Clinical notes always cost **1 credit**.\n"
            "- Adding *Therapist Reflection / Supervision View* uses **2 credits** (flat rate):\n"
            "- Example: clinical notes + deep reflection = **3 credits total**.\n"
            "- Regenerating always counts as a new AI generation.\n"
            "- Credits measure AI usage, not therapy sessions."
        )


    
    st.sidebar.markdown("---")
    st.sidebar.subheader("Hosted mode: download-only")
    st.sidebar.caption("No notes are stored on this server. Use Download to save files to your device.")


    # ========= Main content always renders =========

    # ========= Main content =================
    st.title("FieldNotes - Session Companion")
    st.write(
        "FieldNotes is a Gestalt-informed AI companion to help therapists turn a quick narrative into "
        "clear session notes and supervision material.\n\n"
        "**Hosted download-only mode:** This site does **not** store your notes on the server. "
        "Use the download buttons to save to your own device."
    )

    # ---------------- Main status / welcome messaging (main column) ----------------
    
    subscribe_url = "https://billing.psychotherapist.sg"  # optional: your billing portal domain (or remove)
    trial_request_url = "https://psychotherapist.sg/fieldnotes-contact-form"
    
    # Define subscription state safely
    is_subscribed = (subscription_status or "").lower() in ("active", "trialing")
    
    # Detect “lapsed subscriber” (has Stripe ids but is not active anymore)
    has_paid_history = False
    if pg_user:
        # pg_user shape from pg_get_user:
        # (email, plan, credits_remaining, monthly_allowance, last_reset,
        #  subscription_status, stripe_customer_id, stripe_subscription_id)
        stripe_customer_id = pg_user[6] if len(pg_user) > 6 else None
        stripe_subscription_id = pg_user[7] if len(pg_user) > 7 else None
        has_paid_history = bool(stripe_customer_id or stripe_subscription_id)
    
    # 1) No email yet → ALWAYS show this (and do NOT show “trial ended”)
    if not email_ok:
        st.info("Please input your email in the sidebar to begin.")
    
    # 2) Admin
    elif admin:
        st.success("Admin access: generation enabled.")
    
    # 3) Active subscriber
    elif is_subscribed:
        st.success("Your subscription is live.")
    
    # 4) Trial user with remaining credits
    elif credits_remaining > 0:
        st.info(
            f"You have **{credits_remaining}** credits remaining. "
            "Subscribe for ongoing use (100 credits/month)."
        )
    
        # 👉 PLACE SUBSCRIBE BUTTON HERE
        if st.button("Subscribe here", key=f"subscribe_trial_{user_email}"):
            url = start_stripe_checkout(user_email)
            if url:
                st.session_state["checkout_url"] = url
            else:
                st.error("Could not start Stripe checkout. Please try again.")
    
        checkout_url = st.session_state.get("checkout_url")
        if checkout_url:
            st.link_button("Open secure Stripe checkout", checkout_url)
    
    
    # 5) No credits and not subscribed → differentiate new vs lapsed subscriber vs inactive
    else:
        if created:
            # 1a) Brand new email (just created in DB)
            st.info(
                "Thank you for being here.\n\n"
                "To generate notes, please subscribe, or request a "
                "[**free trial of 7 credits**]("
                f"{trial_request_url}"
                ")."
            )
    
            # 👉 PLACE SUBSCRIBE BUTTON HERE
            if st.button("Subscribe here", key=f"subscribe_new_{user_email}"):
                url = start_stripe_checkout(user_email)
                if url:
                    st.session_state["checkout_url"] = url
                else:
                    st.error("Could not start Stripe checkout. Please try again.")
    
            checkout_url = st.session_state.get("checkout_url")
            if checkout_url:
                st.link_button("Open secure Stripe checkout", checkout_url)
    
    
        elif has_paid_history:
            # 1e) Subscriber who is no longer subscribed
            st.warning(
                "Welcome back. Your subscription is not active. "
                "Please subscribe again to return."
            )
    
            # 👉 PLACE SUBSCRIBE BUTTON HERE
            if st.button("Subscribe again", key=f"subscribe_lapsed_{user_email}"):
                url = start_stripe_checkout(user_email)
                if url:
                    st.session_state["checkout_url"] = url
                else:
                    st.error("Could not start Stripe checkout. Please try again.")
    
            checkout_url = st.session_state.get("checkout_url")
            if checkout_url:
                st.link_button("Open secure Stripe checkout", checkout_url)
    
    
        else:
            # 1f) Inactive (not new, not subscribed)
            st.info(
                "Welcome.\n\n"
                "To generate notes, please subscribe, or request a "
                "[**free trial of 7 credits**]("
                f"{trial_request_url}"
                ")."
            )
    
            # 👉 PLACE SUBSCRIBE BUTTON HERE
            if st.button("Subscribe here", key=f"subscribe_inactive_{user_email}"):
                url = start_stripe_checkout(user_email)
                if url:
                    st.session_state["checkout_url"] = url
                else:
                    st.error("Could not start Stripe checkout. Please try again.")
    
            checkout_url = st.session_state.get("checkout_url")
            if checkout_url:
                st.link_button("Open secure Stripe checkout", checkout_url)
    
    
    # Optional: keep an “Account” line, but only once email exists
    if email_ok:
        st.subheader("Account")
        st.write(f"Signed in as: **{user_email}**")


    # Client label (not stored)
    st.markdown("### 👨🏽‍🦰🧔🏻‍♀️ Client & Session Code")
    client_name = st.text_input(
        "Client label for this session:",
        value="",
        placeholder="e.g. Emma-250313, Couple 03, C-017 (avoid full names if possible)",
    ).strip() or "Unknown client"

    # Session narrative (stored only in session_state in the browser session)
    if "narrative_text" not in st.session_state:
        st.session_state["narrative_text"] = ""

    if "notes_text" not in st.session_state:
        st.session_state["notes_text"] = ""

    if "reflection_text" not in st.session_state:
        st.session_state["reflection_text"] = ""

    st.markdown("### ✍️ Session narrative")
    narrative = st.text_area(
        "Session narrative (Write freely in your own words. Be reflexive.)",
        key="narrative_text",
        height=280,
        placeholder="Write your session details here...",
    )

    st.download_button(
        label="save draft (.txt)",
        data=(narrative or ""),
        file_name="fieldnotes_draft.txt",
        mime="text/plain",
        key="dl_draft_txt"
    )
    # ---------------- Main UI settings (just above Generate button) ----------------
    
    # Output detail level (main UI)
    if access_ok:
        output_mode = st.radio(
            "Clinical Notes Output detail level",
            ["Short", "Full"],
            index=1,
            horizontal=True,
            key="output_mode_main",
        )
    else:
        output_mode = "Full"
                        
    # Reflection toggle (main UI)
    generate_reflection = st.checkbox(
        "Generate therapist reflection / supervision view",
        value=False,
        key="generate_reflection_main",
    )
        
    # Reflection intensity (only if reflection is enabled)
    if generate_reflection:
        reflection_intensity = st.selectbox(
            "Reflection intensity",
            options=["Basic", "Deep", "Very deep"],
            index=1,
            key="reflection_intensity_main",
        )
    else:
        reflection_intensity = "Deep"


    # -------------------------
    # Generation eligibility (must be defined for ALL paths)
    # -------------------------
    is_subscribed = subscription_status in ("active", "trialing")
    
    # Default from session_state (set by the Subscriber PIN UI)
    # Default: subscribers must enter PIN (fail-closed), others default OK
    if is_subscribed and (not admin):
        subscriber_pin_ok = st.session_state.get("subscriber_pin_ok", False)
    else:
        subscriber_pin_ok = st.session_state.get("subscriber_pin_ok", True)


    
  # If you implemented subscriber PIN UI, it should set subscriber_pin_ok accordingly.
    # But even if not, the default above prevents NameError.
    
    # Subscriber PIN default logic (A4)
    if admin or not is_subscribed:
        subscriber_pin_ok = True
    else:
        subscriber_pin_ok = st.session_state.get("subscriber_pin_ok", False)
    
    can_generate = access_ok and (admin or subscriber_pin_ok)

    # Helpful messages (optional)
    if not email_ok:
        st.info("Enter your email in the sidebar to enable generation.")
    elif not access_ok:
        st.warning(
            "Access is locked. "
            "Enter the trial access password in the sidebar or subscribe to continue."
        )
    elif not subscriber_pin_ok:
        st.warning("Please enter your subscriber PIN in the sidebar to generate outputs.")
    elif (not is_subscribed) and (credits_remaining <= 0) and (not admin):
        st.warning("No credits remaining. Please subscribe to continue.")
    
   
    # Subscriber PIN gate default:
    # - Admin: allowed
    # - Non-subscriber: allowed
    # - Subscriber (non-admin): BLOCKED until correct PIN entered
    if admin or not is_subscribed:
        subscriber_pin_ok = True
    else:
        subscriber_pin_ok = st.session_state.get("subscriber_pin_ok", False)


    # ===== Generate button (main area) =====
    if st.button("Generate structured output", disabled=(not can_generate) or st.session_state.get("is_generating", False)):

        # Default: do not generate unless all checks pass
        generate_now = False
    
        if not email_ok:
            st.warning("Please enter your email in the sidebar to continue.")
        elif not access_ok:
            st.warning("Access is locked. Enter a valid trial access code (invite-only) or subscribe.")

        elif (not is_subscribed) and credits_remaining <= 0:
            st.warning("Free trial ended. Please subscribe (USD 29/month) or add credits to continue.")
        elif not narrative.strip():
            st.warning("Please enter a session narrative first.")
        else:
            generate_now = True
    
        if generate_now:
            combined_narrative = narrative
        
            # Enforce subscriber PIN only for active/trialing subscribers (non-admin)
            if is_subscribed and (not admin) and (not subscriber_pin_ok):
                st.warning("Please enter your Subscriber PIN in the sidebar to generate.")
                st.stop()
        
            # ----- Idempotency guard (prevent double-click / rerun spend) -----
            request_payload = f"{user_email}|{client_name}|{output_mode}|{combined_narrative}"
            request_hash = hashlib.sha256(request_payload.encode("utf-8")).hexdigest()
        
            now = time.time()
            last_hash = st.session_state.get("last_request_hash")
            last_time = st.session_state.get("last_request_time", 0)
        
            if last_hash == request_hash and (now - last_time) < 15:
                st.warning("That request was just submitted. Please wait a moment.")
                st.stop()
        
            # Record immediately so fast double-clicks are caught
            st.session_state["last_request_hash"] = request_hash
            st.session_state["last_request_time"] = now
        
            # Clear old notes immediately to avoid stale display on reruns
            st.session_state["notes_text"] = ""
        
            # Lock UI to prevent double-click
            st.session_state["is_generating"] = True
        
            # ----- NOTES: single OpenAI call -----
            try:
                with st.spinner("Generating clinical notes..."):
                    notes_text = call_openai(
                        combined_narrative,
                        client_name,
                        output_mode
                    )
            except Exception as e:
                st.error("OpenAI request failed. No credits were used.")
                st.exception(e)
                st.stop()
            finally:
                # Always unlock UI
                st.session_state["is_generating"] = False
        
            # Deduct ONLY after success
            if not admin:
                if not pg_try_deduct_credits(user_email, COST_GENERATE_NOTES):
                    st.warning("Not enough credits to save this generation. Please top up and try again.")
                    st.stop()
        
            # Save output
            st.session_state["notes_text"] = notes_text
            st.session_state["gen_timestamp"] = datetime.now().strftime("%Y-%m-%d_%H-%M")
        
        
            # ----- REFLECTION (optional) -----
            if generate_reflection:
                st.session_state["reflection_text"] = ""
        
                cost = REFLECTION_COST.get(reflection_intensity, 2)
        
                try:
                    with st.spinner("Generating therapist reflection / supervision view..."):
                        reflection = call_reflection_engine(
                            narrative=combined_narrative,
                            ai_output=notes_text,
                            client_name=client_name,
                            intensity=reflection_intensity,
                        )
                except Exception as e:
                    st.error("Reflection generation failed. No credits were used.")
                    st.exception(e)
                    st.stop()
        
                if not admin:
                    if not pg_try_deduct_credits(user_email, cost):
                        st.warning("Not enough credits to save this reflection.")
                        st.stop()
        
                st.session_state["reflection_text"] = reflection
            else:
                st.session_state["reflection_text"] = ""


    # ALWAYS read from session_state (survives reruns + downloads)
    notes_text = st.session_state.get("notes_text", "")
    reflection_text = st.session_state.get("reflection_text", "")

    # Tabs: Notes / Reflection (only show if we have something)
    if notes_text.strip() or reflection_text.strip():
        st.markdown("---")
        notes_tab, reflection_tab = st.tabs(["Notes", "Reflection"])

        timestamp = st.session_state.get("gen_timestamp") or datetime.now().strftime("%Y-%m-%d_%H-%M")
        safe_name = safe_download_name(client_name)

        with notes_tab:
            st.markdown("### 📝 Clinical notes (AI-structured)")
            st.markdown(
                "You can scroll, select, and copy any part of this into your own clinical notes "
                "or supervision material."
            )
            st.markdown(notes_text)

            st.caption(
                "Reminder: this hosted app does not store notes. "
                "Download to keep a local copy."
            )

            clean_txt = convert_contact_cycle_table_to_prose(notes_text)

            st.download_button(
                label="💾 Download notes as .txt",
                data=clean_txt,
                file_name=f"{safe_name}_{timestamp}_notes.txt",
                mime="text/plain",
                key="dl_notes_txt",
            )

            if notes_text.strip():
                try:
                    pdf_source = convert_contact_cycle_table_to_prose(notes_text)
                    pdf_bytes = create_pdf_from_text(pdf_source)
                    st.download_button(
                        label="📄 Download notes as PDF",
                        data=pdf_bytes,
                        file_name=f"{safe_name}_{timestamp}_notes.pdf",
                        mime="application/pdf",
                        key="dl_notes_pdf",
                    )
                except Exception as e:
                    st.warning("PDF export failed for this output. Please download .txt instead.")
                    st.exception(e)
            else:
                st.warning("No notes to export yet — generate notes first.")

        with reflection_tab:
            st.markdown("### 🧑🏼‍🦳 Therapist reflection / supervision view")

            if reflection_text.strip():             
                
                st.markdown(
                    "For your eyes only – a supervision-style reflection on your process, "
                    "shame arcs, field dynamics, and possible Gestalt experiments."
                )
                st.markdown(reflection_text)

                st.download_button(
                    label="💾 Download reflection as .txt",
                    data=reflection_text,
                    file_name=f"{safe_name}_{timestamp}_reflection.txt",
                    mime="text/plain",
                    key="dl_reflection_txt",
                )

                reflection_pdf = create_pdf_from_text(reflection_text)
                st.download_button(
                    label="📄 Download reflection as PDF",
                    data=reflection_pdf,
                    file_name=f"{safe_name}_{timestamp}_reflection.pdf",
                    mime="application/pdf",
                    key="dl_reflection_pdf",
                )
            else:
                st.write(
                    "No reflection generated for this session. "
                    "Tick the reflection option in the sidebar if you want one next time."
                )
                

    st.caption(f"FieldNotes for Therapists · v{APP_VERSION} · Created by Nicole Chew-Helbig")

if __name__ == "__main__":
    main()
