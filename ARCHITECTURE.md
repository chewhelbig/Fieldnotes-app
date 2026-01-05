# FieldNotes Architecture (High-level)

## Services

### 1) Streamlit App (Therapist UI)
- Purpose: therapist-facing interface and clinical workflow
- Responsibilities:
  - Collect user email / app PIN (if used)
  - Email verification for trial onboarding
  - Generate notes + optional reflection via OpenAI
  - Deduct credits after successful generation
  - Display credits/subscription status
- Database: reads/writes `users` table (Postgres)

### 2) Billing Service (FastAPI)
- Purpose: Stripe subscription lifecycle + billing authority
- Responsibilities:
  - Create Stripe Checkout sessions
  - Create Stripe Billing Portal sessions
  - Receive Stripe webhooks and update Postgres
  - Grant monthly credits on subscription start and renewal
  - Send subscription onboarding emails (transactional)
- Reliability: webhook must never fail due to email issues

## Database
Single Postgres database shared by both services.

Core table: `users`
- email (PK)
- credits_remaining
- plan / subscription_status
- stripe_customer_id / stripe_subscription_id
- email_verified_at
- trial_credits_granted_at (set when trial credits are actually granted)

## Email flows
- Trial verified email: sent by Streamlit after verification succeeds and trial credits are granted.
- Subscription onboarding emails: sent by Billing Service on `checkout.session.completed`.
- Email sending is “best-effort”: failure to send must not break webhook processing.

## Key URLs
- App UI: https://fieldnotes.psychotherapist.sg
- Landing/manual: https://psychotherapist.sg/fieldnotes
