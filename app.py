import os
import io
import re
from datetime import datetime
import streamlit as st
from openai import OpenAI
from fpdf import FPDF
import streamlit.components.v1 as components

import sqlite3
from datetime import date


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

def init_db():
    conn = sqlite3.connect("usage.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            plan TEXT,
            credits_remaining INTEGER,
            monthly_allowance INTEGER,
            last_reset TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()
# ===== User EMAIL ============
def can_generate(email, cost=1):
    conn = sqlite3.connect("usage.db")
    c = conn.cursor()
    c.execute("SELECT credits_remaining FROM users WHERE email=?", (email,))
    row = c.fetchone()
    conn.close()
    return bool(row and row[0] >= cost)


def deduct_credits(email, cost=1):
    conn = sqlite3.connect("usage.db")
    c = conn.cursor()
    c.execute("""
        UPDATE users
        SET credits_remaining = credits_remaining - ?
        WHERE email=?
    """, (cost, email))
    conn.commit()
    conn.close()


def reset_if_needed(email):
    conn = sqlite3.connect("usage.db")
    c = conn.cursor()
    c.execute("SELECT last_reset, monthly_allowance FROM users WHERE email=?", (email,))
    row = c.fetchone()

    if row:
        last_reset, allowance = row
        if date.fromisoformat(last_reset).month != date.today().month:
            c.execute("""
                UPDATE users
                SET credits_remaining=?, last_reset=?
                WHERE email=?
            """, (allowance, date.today().isoformat(), email))

    conn.commit()
    conn.close()

# ============PASSWORD================
def require_app_password():
    pwd = os.environ.get("APP_ACCESS_PASSWORD")

    if not pwd:
        return  # no password set → app open

    if st.session_state.get("access_ok"):
        return

    st.title("FieldNotes")

    with st.form("access_form", clear_on_submit=False):
        # Dummy field to reduce browser password popups
        st.text_input(
            "Username",
            value="",
            key="__dummy_user",
            label_visibility="collapsed"
        )

        entered = st.text_input(
            "Enter access password",
            type="password",
            key="access_password"
        )

        submitted = st.form_submit_button("Enter")

    if submitted:
        if entered == pwd:
            st.session_state["access_ok"] = True
            st.rerun()
        else:
            st.error("Incorrect password")

    st.stop()



# ------Get OPEN AI------------
@st.cache_resource
def get_openai_client():
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    return OpenAI(api_key=key)

    client = get_openai_client()
    if client is None:
        st.sidebar.error("Server is missing OPENAI_API_KEY (Render env var).")

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

# ==================
# Credits
#===================

COST_GENERATE_NOTES = 1
REFLECTION_COST = {
    "Basic": 1,
    "Medium": 1,
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
def ensure_user_exists(email: str):
    conn = sqlite3.connect("usage.db")
    c = conn.cursor()
    c.execute("SELECT email FROM users WHERE email=?", (email,))
    if not c.fetchone():
        c.execute("""
            INSERT INTO users (email, plan, credits_remaining, monthly_allowance, last_reset)
            VALUES (?, ?, ?, ?, ?)
        """, (email, "monthly", 30, 30, date.today().isoformat()))
    conn.commit()
    conn.close()
# =====================
# =======UI============
def main():
    require_app_password() 
        

    # ========= Sidebar: account ===========
    st.sidebar.markdown("### 👤 Account")
    user_email = st.sidebar.text_input(
        "Email (for subscription & credits)",
        value=st.session_state.get("user_email", ""),
        placeholder="you@clinic.com",
    ).strip().lower()

    email_ok = bool(user_email)

    if not email_ok:
        st.sidebar.info("Enter your email to enable credits & downloads.")
    else:
        st.session_state["user_email"] = user_email
        ensure_user_exists(user_email)
        reset_if_needed(user_email)

        # ---- usage explanation ----
        st.sidebar.markdown("---")
        st.sidebar.subheader("Usage")
    
        with st.sidebar.expander("What is 1 generation?"):
            st.markdown(
                "- **1 generation = 1 click on “Generate structured output”.**\n"
                "- Includes clinical notes and reflection (if enabled).\n"
                "- Regenerating counts as a new generation.\n"
                "- A generation is an AI output, not a therapy session."
            )

    # ---- sidebar settings ----
    st.sidebar.header("Settings")
    output_mode = st.sidebar.radio("Output detail level", ["Short", "Full"], index=1)

    generate_reflection = st.sidebar.checkbox(
        "Generate therapist reflection / supervision view",
        value=False,
    )

    if generate_reflection:
        reflection_intensity = st.sidebar.selectbox(
            "Reflection intensity",
            options=["Basic", "Deep", "Very deep"],
            index=1,
        )
    else:
        reflection_intensity = "Deep"

    # ---- sidebar info ----
    st.sidebar.markdown("---")
    st.sidebar.subheader("Hosted mode: download-only")
    st.sidebar.caption("No notes are stored on this server. Use Download to save files to your device.")

    st.sidebar.markdown("---")
    st.sidebar.subheader("About")
    st.sidebar.caption(f"FieldNotes for Therapists · v{APP_VERSION}")
    st.sidebar.caption("Created by Nicole Chew-Helbig, Gestalt psychotherapist")
    st.sidebar.caption(
        "These notes are generated to support your clinical thinking and are not a "
        "substitute for your professional judgment or supervision."
    )

    # ========= Main content =================
    st.title("FieldNotes - Session Companion")
    st.write(
        "FieldNotes is a Gestalt-informed AI companion to help therapists turn a quick narrative into "
        "clear session notes and supervision material.\n\n"
        "**Hosted download-only mode:** This site does **not** store your notes on the server. "
        "Use the download buttons to save to your own device."
    )

    if not email_ok:
        st.info("To start: enter your email in the sidebar (used only for credits & billing).")

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

    # ===== Generate button (main area) =====
    if st.button("Generate structured output", disabled=not email_ok):
    
        if not email_ok:
            st.warning("Please enter your email in the sidebar to continue.")
            st.stop()
    
        if not narrative.strip():
            st.warning("Please enter a session narrative first.")
            st.stop()
    
        combined_narrative = narrative
    
        # 1) NOTES — check credits first
        if not can_generate(user_email, COST_GENERATE_NOTES):
            st.warning("Not enough credits to generate notes. Please top up.")
            st.stop()
    
        with st.spinner("Generating clinical notes..."):
            notes_text = call_openai(
                combined_narrative,
                client_name,
                output_mode
            )
    
        st.session_state["notes_text"] = notes_text
        deduct_credits(user_email, COST_GENERATE_NOTES)
        st.session_state["gen_timestamp"] = datetime.now().strftime("%Y-%m-%d_%H-%M")
    
        # 2) REFLECTION (optional)
        if generate_reflection:
            cost = REFLECTION_COST.get(reflection_intensity, 1)
    
            if not can_generate(user_email, cost):
                st.warning("Not enough credits to generate reflection. Please top up.")
                st.stop()
    
            with st.spinner("Generating therapist reflection / supervision view..."):
                reflection = call_reflection_engine(
                    narrative=combined_narrative,
                    ai_output=notes_text,
                    client_name=client_name,
                    intensity=reflection_intensity,
                )
    
            st.session_state["reflection_text"] = reflection
            deduct_credits(user_email, cost)
        else:
            st.session_state["reflection_text"] = ""


    # ALWAYS read from session_state (survives reruns + downloads)
    notes_text = st.session_state["notes_text"]
    reflection_text = st.session_state["reflection_text"]

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
                pdf_source = convert_contact_cycle_table_to_prose(notes_text)
                pdf_bytes = create_pdf_from_text(pdf_source)
            
                st.download_button(
                    label="📄 Download notes as PDF",
                    data=pdf_bytes,
                    file_name=f"{safe_name}_{timestamp}_notes.pdf",
                    mime="application/pdf",
                    key="dl_notes_pdf",
                )

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

    st.write("OpenAI client ready:", get_openai_client() is not None)

    st.caption(f"FieldNotes for Therapists · v{APP_VERSION} · Created by Nicole Chew-Helbig")

if __name__ == "__main__":
    main()
