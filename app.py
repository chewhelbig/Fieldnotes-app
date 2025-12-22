import os
import json
import io
from datetime import datetime
import re  # for extracting section A later

import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI
from fpdf import FPDF
import zipfile


# ---------- Sidebar OpenAI key UI (Option A) ----------
def sidebar_openai_key_ui() -> None:
    """Show OpenAI key input in sidebar; store it only in this session."""
    if "user_openai_key" not in st.session_state:
        st.session_state["user_openai_key"] = ""

    with st.sidebar:
        st.markdown("### 🔑 OpenAI API key")
        st.text_input(
            "Enter your OpenAI API key",
            type="password",
            key="user_openai_key",
            placeholder="sk-...",
            help="Stored only in this browser session. Not saved to disk.",
        )

        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button("Clear key"):
                st.session_state["user_openai_key"] = ""
                st.rerun()
        with col2:
            st.caption("✅ Ready" if st.session_state.get("user_openai_key") else "Enter key")


def get_openai_client_or_none():
    """Option A: return client if key exists, else None."""
    key = (st.session_state.get("user_openai_key") or "").strip()
    if not key:
        return None
    return OpenAI(api_key=key)


# -------------------------------
# App configuration constants
# -------------------------------
OPENAI_MODEL_NOTES = "gpt-4.1-mini"
OPENAI_MODEL_REFLECTION = "gpt-4.1-mini"
MAX_TOKENS_REFLECTION = 2300

DEFAULT_SESSIONS_ROOT = "client_sessions"
CLIENTS_FILE = "clients.json"

APP_VERSION = "0.1"


def get_sessions_root() -> str:
    """
    Return the current root folder for session files.
    Uses Streamlit session_state so advanced users can change it.
    """
    return st.session_state.get("SESSIONS_ROOT", DEFAULT_SESSIONS_ROOT)


# -------------------------------
# Client list storage (local file)
# -------------------------------
def load_clients():
    if os.path.exists(CLIENTS_FILE):
        try:
            with open(CLIENTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return sorted(set(data))
        except Exception as e:
            st.warning(f"Could not load clients list: {e}")
    return []


def save_clients(clients):
    """Save client names to a local JSON file."""
    try:
        with open(CLIENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(set(clients)), f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# -------------------------------
# Session note storage (local folders)
# -------------------------------
def safe_client_folder_name(name: str) -> str:
    """Turn client name into a safe folder name without using regex."""
    if not name or not name.strip():
        return "unknown_client"

    allowed_chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_- "
    cleaned = "".join(c for c in name.strip() if c in allowed_chars)

    # Replace spaces with underscores
    cleaned = cleaned.replace(" ", "_")

    return cleaned or "client"


def save_session_note_to_folder(client_name: str, content: str) -> str:
    """
    Save the generated note into a client-specific folder as plain text (.txt).
    """
    safe_name = safe_client_folder_name(client_name)
    client_dir = os.path.join(get_sessions_root(), safe_name)
    os.makedirs(client_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    filename = f"session_{timestamp}.txt"
    file_path = os.path.join(client_dir, filename)

    try:
        with open(file_path, "w", encoding="utf-8") as f_out:
            f_out.write(content)
    except Exception:
        # Fail silently; do not crash if writing fails
        return file_path

    return file_path


def list_client_sessions(client_name: str) -> list[tuple[str, str]]:
    safe_name = safe_client_folder_name(client_name)
    client_dir = os.path.join(get_sessions_root(), safe_name)
    if not os.path.isdir(client_dir):
        return []

    files = []
    for fname in os.listdir(client_dir):
        if fname.endswith(".txt"):
            full_path = os.path.join(client_dir, fname)
            files.append((fname, full_path))

    files.sort(key=lambda x: x[0])
    return files


def load_session_content(file_path: str) -> str:
    """
    Load raw text from a saved session or reflection file.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Could not load session content.\n\nError: {e}"


def remove_markdown_tables(text: str) -> str:
    """Remove any Markdown table (lines starting with '|') from plain text output."""
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        if line.strip().startswith("|"):
            continue   # skip all table rows
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


# --------------------------------
# Extract A Section
# -------------------------------
def extract_assessment_section(text: str) -> str:
    """
    Extract the 'A (Analysis/Assessment)' section from a SOAP note.
    """
    lines = text.splitlines()
    start_idx = None

    # 1. Find the line where the A-section starts
    for i, line in enumerate(lines):
        stripped = line.strip()
        stripped = stripped.lstrip("#>*- ").strip()

        if stripped.startswith("A (") and ("Analysis" in stripped or "Assessment" in stripped):
            start_idx = i
            break

    if start_idx is None:
        return text.strip()

    # 2. Find where the next SOAP section starts (S, O, or P)
    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        stripped = lines[j].strip()
        stripped_no_md = stripped.lstrip("#>*- ").strip()
        if (
            stripped_no_md.startswith("S (")
            or stripped_no_md.startswith("O (")
            or stripped_no_md.startswith("P (")
        ):
            end_idx = j
            break

    section = "\n".join(lines[start_idx:end_idx])
    return section.strip()


def extract_soap_section(text: str) -> str:
    """
    Extract ONLY the Gestalt-style SOAP note section from the full AI output.
    """
    lines = text.splitlines()
    start_idx = None

    # 1. Find where the SOAP section starts
    for i, line in enumerate(lines):
        stripped = line.strip()
        stripped = stripped.lstrip("#>*- ").strip()
        upper = stripped.upper()
        if "GESTALT-STYLE SOAP NOTE" in upper:
            start_idx = i
            break

    # Fallback: if we don't see the SOAP heading, try from "S (Subjective)"
    if start_idx is None:
        for i, line in enumerate(lines):
            stripped = line.strip().lstrip("#>*- ").strip()
            if stripped.startswith("S (Subjective"):
                start_idx = i
                break

    if start_idx is None:
        return text.strip()

    # 2. Find where SOAP ends (before Supervisor-Style Questions / section 3)
    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        stripped = lines[j].strip()
        stripped_no_md = stripped.lstrip("#>*- ").strip()
        upper = stripped_no_md.upper()

        if ("SUPERVISOR-STYLE QUESTIONS" in upper) or upper.startswith("3) SUPERVISOR"):
            end_idx = j
            break
        if upper.startswith("3) ") and "SUPERVISOR" in upper:
            end_idx = j
            break

    section = "\n".join(lines[start_idx:end_idx])
    return section.strip()


def get_client_history_snippet(client_name: str, max_sessions: int = 3) -> str:
    """
    Load the last few session files for a client and pull out ONLY the SOAP
    'A = Assessment' section from each session.
    """
    safe_name = safe_client_folder_name(client_name or "Unknown_client")
    client_folder = os.path.join(get_sessions_root(), safe_name)
    if not os.path.exists(client_folder):
        return ""

    files = [
        f for f in os.listdir(client_folder)
        if f.startswith("session_") and f.endswith(".txt")
    ]
    if not files:
        return ""

    files.sort(reverse=True)  # latest first

    snippets = []
    for fname in files[:max_sessions]:
        path = os.path.join(client_folder, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            assessment = extract_assessment_section(content)
            if assessment:
                snippets.append(f"From {fname} (SOAP – A: Assessment):\n{assessment}")
        except Exception:
            continue

    return "\n\n".join(snippets)


# -------------------------------
# Contact cycle table → text helper
# -------------------------------
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


# -------------------------------
# PDF creation helper
# -------------------------------
def create_pdf_from_text(content: str) -> bytes:
    """Create a simple, readable PDF from the AI text."""
    safe_content = (
        content.replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("–", "-")
        .replace("—", "-")
    )

    lines = safe_content.split("\n")
    new_lines: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("|") and "Phase of Contact Cycle" in stripped:
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            table_text = contact_cycle_table_to_text(table_lines)
            if table_text:
                new_lines.append(table_text)
                new_lines.append("")
        else:
            new_lines.append(lines[i])
            i += 1

    safe_content = "\n".join(new_lines)

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Arial", size=11)

    page_width = pdf.w - 2 * pdf.l_margin
    paragraphs = safe_content.split("\n\n")

    for para in paragraphs:
        para = para.strip()
        if not para:
            pdf.ln(4)
            continue

        para = " ".join(para.splitlines())
        para = para.replace("**", "")

        pdf.multi_cell(page_width, 6, para)
        pdf.ln(4)

    pdf_bytes = pdf.output(dest="S")
    if isinstance(pdf_bytes, bytearray):
        pdf_bytes = bytes(pdf_bytes)
    return pdf_bytes


# -------------------------------
# System prompt (the "brain")
# -------------------------------
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


# -------------------------------
# OpenAI call helpers
# -------------------------------
# ---------- OpenAI call for structured notes ----------
def call_openai(combined_narrative: str, client_name: str, output_mode: str) -> str:
    user_prompt = build_prompt(combined_narrative, client_name, output_mode)

    try:
        # 🔒 Gate AI usage here
        client = get_openai_client_or_none()
        if client is None:
            st.error("Please enter your OpenAI API key in the sidebar to use AI features.")
            st.stop()

        response = client.chat.completions.create(
            model=OPENAI_MODEL_NOTES,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content

    except Exception as e:
        st.error("⚠️ There was a problem contacting the AI model.")
        st.caption(f"Technical details: {e}")
        return "Error: The AI could not generate output. Please try again."

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

    try:
        # 🔒 Gate AI usage here
        client = get_openai_client_or_none()
        if client is None:
            st.error("Please enter your OpenAI API key in the sidebar to use AI features.")
            st.stop()

        response = client.chat.completions.create(
            model=OPENAI_MODEL_NOTES,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content
    except Exception as e:
        st.error("⚠️ There was a problem contacting the AI model.")
        st.caption(f"Technical details: {e}")
        return "Error: The AI could not generate output. Please try again."


# ---------- OpenAI call for reflection engine ----------
def call_reflection_engine(
    narrative: str,
    ai_output: str,
    client_name: str,
    intensity: str,
) -> str:
    user_prompt = build_reflection_prompt(
        narrative=narrative,
        ai_output=ai_output,
        client_name=client_name,
        intensity=intensity,
    )

    try:
        # 🔒 Gate AI usage here
        client = get_openai_client_or_none()
        if client is None:
            st.error("Please enter your OpenAI API key in the sidebar to use AI features.")
            st.stop()

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

    except Exception as e:
        st.error("⚠️ There was a problem contacting the AI model.")
        st.caption(f"Technical details: {e}")
        return "Error: The AI could not generate reflection output. Please try again."

    intensity_instructions = REFLECTION_INTENSITY_INSTRUCTIONS.get(
        intensity, REFLECTION_INTENSITY_INSTRUCTIONS["Deep"]
    )

    user_prompt = f"""
You are helping the therapist reflect on their own process in relation to one client.

Client name (for context only): {client_name or "Unknown client"}.

Below is:
1) The therapist's raw narrative of the session (with possible contextual info).
2) The structured notes that were generated (clean narrative, SOAP, etc.).

First, read the REFLECTION SYSTEM PROMPT (your role and style).
Second, pay attention to the reflection intensity:

Reflection intensity: {intensity}
Instructions for depth:
{intensity_instructions}

Then produce a supervision-style reflection following the structure in the REFLECTION SYSTEM PROMPT.

----------------
(1) RAW NARRATIVE
----------------
{narrative}

-----------------------
(2) STRUCTURED AI NOTES
-----------------------
{ai_output}
"""

    try:
        # 🔒 Gate AI usage here
        client = get_openai_client_or_none()
        if client is None:
            st.error("Please enter your OpenAI API key in the sidebar to use AI features.")
            st.stop()
    
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
    except Exception as e:
        st.error("⚠️ Reflection engine failed.")
        st.caption(f"Technical details: {e}")
        return "Reflection could not be generated due to an AI error."


def save_reflection_note_to_folder(client_name: str, content: str) -> str:
    """
    Save the therapist reflection to the client's folder
    as a separate file, e.g. reflection_YYYY-MM-DD_HH-MM.txt
    """
    safe_name = safe_client_folder_name(client_name or "Unknown_client")
    client_folder = os.path.join(get_sessions_root(), safe_name)
    os.makedirs(client_folder, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    filename = f"reflection_{timestamp}.txt"
    file_path = os.path.join(client_folder, filename)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

    return file_path


# -------------------------------
# Streamlit app
# -------------------------------
def main():
    st.set_page_config(
        page_title="FieldNotes for Therapists",
        layout="centered"
    )

    # ✅ Add this line here (early, before your own sidebar block)
    sidebar_openai_key_ui()

    # Ensure session root is initialised
    if "SESSIONS_ROOT" not in st.session_state:
        st.session_state["SESSIONS_ROOT"] = DEFAULT_SESSIONS_ROOT

    # ---------------- Sidebar ----------------
    with st.sidebar:
        st.header("Settings")

        # Output detail level
        output_mode = st.radio(
            "Output detail level",
            ["Short", "Full"],
            index=1,
            help="Short = clean narrative + brief SOAP. Full = all sections including questions, contact cycle, unfinished business."
        )

        # Reflection settings
        generate_reflection = st.checkbox(
            "Generate therapist reflection / supervision view",
            value=False,
            help="Uses the session narrative + generated notes to create a supervision-style reflection just for you."
        )

        if generate_reflection:
            reflection_intensity = st.selectbox(
                "Reflection intensity",
                options=["Basic", "Deep", "Very deep"],
                index=1,
                help="Basic = brief highlights, Deep = fuller reflection, Very deep = more layered supervision-style exploration.",
            )
        else:
            reflection_intensity = "Deep"

        # Previous-session history toggle
        show_history = st.checkbox(
            "Show previous sessions for this client (SOAP only)",
            value=False,
            help="View and optionally delete past notes for this client."
        )

        st.markdown("---")
        st.subheader("Storage location")

        current_root = st.session_state["SESSIONS_ROOT"]
        new_root = st.text_input(
            "Folder for saving all client notes:",
            value=current_root,
            help="Choose where your client folders are stored (e.g. an encrypted folder). "
                 "Existing files are not moved automatically.",
        )

        if st.button("Update storage folder"):
            st.session_state["SESSIONS_ROOT"] = new_root
            st.success(f"Storage root updated to: {new_root}")

        st.caption(f"Active folder: `{get_sessions_root()}`")

        st.markdown("---")
        st.subheader("About")
        st.caption(f"FieldNotes for Therapists · v{APP_VERSION}")
        st.caption("Created by Nicole Chew-Helbig, Gestalt psychotherapist")

        st.caption(
            "These notes are generated to support your clinical thinking and are not a "
            "substitute for your professional judgment or supervision."
        )

    # ---------------- Main content: title & intro ----------------
    st.title("FieldNotes - Session Companion")
    st.write(
        "FieldNotes is a Gestalt-informed AI companion to help you turn a quick narrative into "
        "clear session notes and supervision material.\n\n"
        "Paste your session narrative, and you'll receive a cleaned narrative, "
        "Gestalt-style SOAP note, supervisor-style questions, a contact cycle roadmap, "
        "and possible unfinished business (in full mode).\n\n"
        "**No notes are stored in any external database.** The text you enter is sent securely to the OpenAI API to generate notes. "
        "The app then saves plain-text files only on this computer, in local folders you can see and manage."
    )

    # ---------------- Client selection ----------------
    st.markdown("### 🧑‍🤝‍🧑 Client selection")

    clients = load_clients()

    selected_client = st.selectbox(
        "Select existing client (or leave blank to add new):",
        [""] + clients,
        format_func=lambda x: "— none —" if x == "" else x,
    )

    new_client = st.text_input(
        "New client name (if this is a new client):",
        value="",
        placeholder="e.g. Emma, Couple 03, C-017..."
    ).strip()

    if new_client:
        client_name = new_client
        if new_client not in clients:
            clients.append(new_client)
            save_clients(clients)
    elif selected_client:
        client_name = selected_client
    else:
        client_name = "Unknown client"   # safe default

    # ---------------- Styling for history viewer ----------------
    st.markdown("""
    <style>
    .readonly-box {
        background-color: #f7f7f9;
        color: #222222;
        padding: 1rem;
        border-radius: 8px;
        border: 1px solid #e1e1e1;
        font-size: 0.95rem;
        line-height: 1.45;
        white-space: pre-wrap;
        font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
    }
    </style>
    """, unsafe_allow_html=True)

    # ---------------- Session narrative ----------------
    if "narrative_text" not in st.session_state:
        st.session_state["narrative_text"] = ""

    st.markdown("### ✍️ Session narrative")
    narrative = st.text_area(
        "Session narrative (write in your own words)",
        key="narrative_text",
        height=280,
        placeholder="Write your session details here..."
    )

    notes_text = None
    reflection_text = None

    # ---------------- Generate button (above history) ----------------
    if st.button("Generate structured output"):
        if not narrative.strip():
            st.warning("Please enter a session narrative first.")
            st.stop()

        client_history = get_client_history_snippet(client_name, max_sessions=3)
        if client_history:
            combined_narrative = f"""PAST SESSION SUMMARIES (Assessment sections only, for context):
{client_history}

CURRENT SESSION NARRATIVE:
{narrative}
"""
        else:
            combined_narrative = narrative

        with st.spinner("Generating clinical notes..."):
            notes_text = call_openai(combined_narrative, client_name, output_mode)

        # Save main session notes
        save_session_note_to_folder(client_name, notes_text)

        # Optional reflection
        if generate_reflection:
            with st.spinner("Generating therapist reflection / supervision view..."):
                reflection_text = call_reflection_engine(
                    narrative=combined_narrative,
                    ai_output=notes_text,
                    client_name=client_name,
                    intensity=reflection_intensity,
                )
            save_reflection_note_to_folder(client_name, reflection_text)
        else:
            reflection_text = None

        # -------- Tabs: Notes / Reflection --------
        st.markdown("---")
        notes_tab, reflection_tab = st.tabs(["Notes", "Reflection"])

        with notes_tab:
            st.markdown("### 📝 Clinical notes (AI-structured)")
            st.markdown(
                "You can scroll, select, and copy any part of this into your own clinical notes "
                "or supervision material."
            )
            st.markdown(notes_text)

            st.success(f"Session notes saved for {client_name}.")

            st.caption(
                "These notes are generated to support your clinical thinking and are not a substitute "
                "for your professional judgment or supervision."
            )

            safe_name = safe_client_folder_name(client_name)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")

            # TXT download (tables stripped)
            clean_txt = remove_markdown_tables(notes_text)
            st.download_button(
                label="💾 Download notes as .txt",
                data=clean_txt,
                file_name=f"{safe_name}_{timestamp}.txt",
                mime="text/plain",
            )

            # PDF download
            pdf_bytes = create_pdf_from_text(notes_text)
            st.download_button(
                label="📄 Download notes as PDF",
                data=pdf_bytes,
                file_name=f"{safe_name}_{timestamp}.pdf",
                mime="application/pdf",
            )

            # ZIP: all notes for this client
            client_folder = os.path.join(get_sessions_root(), safe_name)
            if os.path.isdir(client_folder):
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    for root, dirs, files in os.walk(client_folder):
                        for filename in files:
                            file_path = os.path.join(root, filename)
                            arcname = os.path.relpath(file_path, client_folder)
                            zf.write(file_path, arcname)
                zip_buffer.seek(0)

                st.download_button(
                    label="📦 Download ALL notes for this client (.zip)",
                    data=zip_buffer,
                    file_name=f"{safe_name}_all_notes.zip",
                    mime="application/zip",
                )
            else:
                st.caption("No folder found yet for this client to export as ZIP.")

        with reflection_tab:
            st.markdown("### 🧠 Therapist reflection / supervision view")

            if reflection_text:
                st.markdown(
                    "For your eyes only – a supervision-style reflection on your process, "
                    "shame arcs, field dynamics, and possible Gestalt experiments."
                )
                st.markdown(reflection_text)
                st.info("Reflection saved as a separate file in the same client folder.")
            else:
                st.write(
                    "No reflection generated for this session. "
                    "Tick the reflection option in the sidebar if you want one next time."
                )

    # ---------------- Previous-session viewer (SOAP only + delete) ----------------
    if show_history:
        st.markdown("### 📚 Previous sessions – SOAP notes")

        sessions = list_client_sessions(client_name)
        sessions = sessions[-30:]  # last 30 only

        if sessions:
            labels = []
            label_to_path = {}

            for fname, path in sessions:
                label = fname
                if fname.startswith("session_") and fname.endswith(".txt"):
                    core = fname[len("session_"):-len(".txt")]
                    try:
                        dt = datetime.strptime(core, "%Y-%m-%d_%H-%M")
                        label = dt.strftime("%Y-%m-%d %H:%M")
                    except ValueError:
                        label = fname

                labels.append(label)
                label_to_path[label] = path

            labels_sorted = list(reversed(labels))

            selected_label = st.selectbox(
                "Select a past note file to view (session or reflection):",
                ["— select —"] + labels_sorted,
            )

            if selected_label != "— select —":
                selected_path = label_to_path.get(selected_label)
                if selected_path:
                    full_text = load_session_content(selected_path)
                    soap_only = extract_soap_section(full_text)

                    st.markdown(
                        "<div class='readonly-box'>"
                        + soap_only.replace("\n", "<br>")
                        + "</div>",
                        unsafe_allow_html=True,
                    )

                    # Delete this file
                    st.markdown("#### 🗑 Delete this note file")
                    delete_confirm = st.checkbox(
                        "Yes, permanently delete this file",
                        key=f"delete_confirm_{selected_label}",
                    )
                    if st.button(
                        "Delete selected note file",
                        key=f"delete_button_{selected_label}",
                    ):
                        if delete_confirm:
                            try:
                                os.remove(selected_path)
                                st.success("Note file deleted.")
                                st.experimental_rerun()
                            except Exception as e:
                                st.error(f"Could not delete file: {e}")
                        else:
                            st.warning("Please tick the confirmation box before deleting.")
        else:
            st.caption("No previous notes saved yet for this client.")

    st.caption(f"FieldNotes for Therapists · v{APP_VERSION} · Created by Nicole Chew-Helbig")


if __name__ == "__main__":
    main()
