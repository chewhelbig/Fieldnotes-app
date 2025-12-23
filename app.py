import os
import io
import re
from datetime import datetime

import streamlit as st
from openai import OpenAI
from fpdf import FPDF
import streamlit.components.v1 as components
from streamlit_local_storage import LocalStorage




# =========================
# Hosted / download-only mode
# =========================
APP_VERSION = "0.2-hosted-download-only"

OPENAI_MODEL_NOTES = "gpt-4.1-mini"
OPENAI_MODEL_REFLECTION = "gpt-4.1-mini"
MAX_TOKENS_REFLECTION = 2300

LOCALSTORAGE_KEY_NAME = "fieldnotes_openai_api_key_v1"


LOCALSTORAGE_KEY_NAME = "fieldnotes_openai_api_key_v1"

def get_openai_client_or_none():
    """
    Return an OpenAI client if the user has entered and confirmed an API key.
    Otherwise return None.
    """
    key = (st.session_state.get("user_openai_key") or "").strip()

    if not key:
        return None

    if not st.session_state.get("openai_key_confirmed"):
        return None

    try:
        return OpenAI(api_key=key)
    except Exception:
        return None


def sidebar_openai_key_ui() -> None:
    if "user_openai_key" not in st.session_state:
        st.session_state["user_openai_key"] = ""

    if "openai_key_confirmed" not in st.session_state:
        st.session_state["openai_key_confirmed"] = False

    if "remember_key" not in st.session_state:
        st.session_state["remember_key"] = True  # default on for UX

    localS = LocalStorage()

    restored = localS.getItem(LOCALSTORAGE_KEY_NAME)
    if isinstance(restored, str) and restored.startswith("sk-") and not st.session_state.get("user_openai_key"):
        st.session_state["user_openai_key"] = restored
        # keep confirmed False until user clicks Enter key


    with st.sidebar:
        st.markdown("### 🔑 OpenAI API key")

        with st.expander("Where do I get this key? (2 minutes)", expanded=False):
            st.markdown(
                """
FieldNotes uses OpenAI’s models to generate notes.  
You use your own OpenAI account, and OpenAI bills you directly (usually cents per session).  
This app does not store your notes on the server.

**Steps**
1) Open the OpenAI platform website and sign in: https://platform.openai.com/api-keys  
2) Go to **API keys**  
3) Create a new secret key  
4) Paste it here
                """.strip()
            )

        # Always keep as string (prevents widget crashes)
        st.session_state["user_openai_key"] = str(st.session_state.get("user_openai_key") or "")

        st.text_input(
            "Paste your OpenAI API key",
            type="password",
            key="user_openai_key",
            placeholder="sk-...",
        )

        st.checkbox(
            "Remember on this device",
            key="remember_key",
            help="Stores the key in your browser only (localStorage). Turn off on shared computers.",
        )

        if st.button("Enter key"):
            key = (st.session_state.get("user_openai_key") or "").strip()
            if not key:
                st.session_state["openai_key_confirmed"] = False
                st.warning("Please paste your OpenAI API key first.")
            else:
                st.session_state["openai_key_confirmed"] = True
                if st.session_state.get("remember_key"):
                    localS.setItem(LOCALSTORAGE_KEY_NAME, key)
                st.success("✅ Key saved.")

        st.caption("✅ Ready" if st.session_state.get("openai_key_confirmed") else "Enter key to enable AI features")




# =========================
# Text/PDF helpers
# =========================
def remove_markdown_tables(text: str) -> str:
    """Remove any Markdown table (lines starting with '|') from plain text output."""
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        if line.strip().startswith("|"):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


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

        pdf_out = pdf.output(dest="S")

        # fpdf (pyfpdf) often returns a *str* here; Streamlit needs bytes
        if isinstance(pdf_out, str):
            pdf_out = pdf_out.encode("latin-1", errors="ignore")
    
        # fpdf2 may return bytearray
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


# =========================
# OpenAI call helpers
# =========================
def call_openai(narrative: str, client_name: str, output_mode: str) -> str:
    user_prompt = build_prompt(narrative, client_name, output_mode)

    try:
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


# =========================
# Streamlit app
# =========================
def main():
    st.set_page_config(page_title="FieldNotes for Therapists", layout="centered")

    # Sidebar: key input + settings
    sidebar_openai_key_ui()

    with st.sidebar:
        st.header("Settings")

        output_mode = st.radio(
            "Output detail level",
            ["Short", "Full"],
            index=1,
            help="Short = clean narrative + brief SOAP. Full = all sections including questions, contact cycle, unfinished business.",
        )

        generate_reflection = st.checkbox(
            "Generate therapist reflection / supervision view",
            value=False,
            help="Uses the session narrative + generated notes to create a supervision-style reflection just for you.",
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

        st.markdown("---")
        st.subheader("Hosted mode: download-only")
        st.caption("No notes are stored on this server. Use Download to save files to your device.")

        st.markdown("---")
        st.subheader("About")
        st.caption(f"FieldNotes for Therapists · v{APP_VERSION}")
        st.caption("Created by Nicole Chew-Helbig, Gestalt psychotherapist")
        st.caption(
            "These notes are generated to support your clinical thinking and are not a "
            "substitute for your professional judgment or supervision."
        )

    # Main content
    st.title("FieldNotes - Session Companion")
    st.write(
        "FieldNotes is a Gestalt-informed AI companion to help you turn a quick narrative into "
        "clear session notes and supervision material.\n\n"
        "**Hosted download-only mode:** This site does **not** store your notes on the server. "
        "Your text is sent to the OpenAI API to generate output, then displayed here. "
        "Use the download buttons to save to your own device."
    )

    # Client label (not stored)
    st.markdown("### 🧑‍🤝‍🧑 Client name (typed each time — not stored)")
    client_name = st.text_input(
        "Client label for this session:",
        value="",
        placeholder="e.g. Emma, Couple 03, C-017 (avoid full names if possible)",
    ).strip() or "Unknown client"

    # Session narrative
    if "narrative_text" not in st.session_state:
        st.session_state["narrative_text"] = ""

    st.markdown("### ✍️ Session narrative")
    narrative = st.text_area(
        "Session narrative (write in your own words)",
        key="narrative_text",
        height=280,
        placeholder="Write your session details here...",
    )

    st.markdown("#### Draft safety")
    st.download_button(
        "⬇️ Download draft (txt)",
        data=(narrative or ""),
        file_name="fieldnotes_draft.txt",
        mime="text/plain",
        help="Download a copy of what you typed so far. Useful if the page refreshes or disconnects.",
    )
    
    notes_text = None
    reflection_text = None

    if st.button("Generate structured output"):
        if not narrative.strip():
            st.warning("Please enter a session narrative first.")
            st.stop()

        combined_narrative = narrative  # no history recall in hosted mode

        with st.spinner("Generating clinical notes..."):
            notes_text = call_openai(combined_narrative, client_name, output_mode)

        if generate_reflection:
            with st.spinner("Generating therapist reflection / supervision view..."):
                reflection_text = call_reflection_engine(
                    narrative=combined_narrative,
                    ai_output=notes_text,
                    client_name=client_name,
                    intensity=reflection_intensity,
                )
        else:
            reflection_text = None

        # Tabs: Notes / Reflection
        st.markdown("---")
        notes_tab, reflection_tab = st.tabs(["Notes", "Reflection"])

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
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

            # TXT download (tables stripped)
            clean_txt = remove_markdown_tables(notes_text)
            st.download_button(
                label="💾 Download notes as .txt",
                data=clean_txt,
                file_name=f"{safe_name}_{timestamp}_notes.txt",
                mime="text/plain",
            )

            # PDF download
            pdf_bytes = create_pdf_from_text(notes_text)
            st.download_button(
                label="📄 Download notes as PDF",
                data=pdf_bytes,
                file_name=f"{safe_name}_{timestamp}_notes.pdf",
                mime="application/pdf",
            )

        with reflection_tab:
            st.markdown("### 🧠 Therapist reflection / supervision view")

            if reflection_text:
                st.markdown(
                    "For your eyes only – a supervision-style reflection on your process, "
                    "shame arcs, field dynamics, and possible Gestalt experiments."
                )
                st.markdown(reflection_text)

                # Reflection downloads
                st.download_button(
                    label="💾 Download reflection as .txt",
                    data=reflection_text,
                    file_name=f"{safe_name}_{timestamp}_reflection.txt",
                    mime="text/plain",
                )

                reflection_pdf = create_pdf_from_text(reflection_text)
                st.download_button(
                    label="📄 Download reflection as PDF",
                    data=reflection_pdf,
                    file_name=f"{safe_name}_{timestamp}_reflection.pdf",
                    mime="application/pdf",
                )
            else:
                st.write(
                    "No reflection generated for this session. "
                    "Tick the reflection option in the sidebar if you want one next time."
                )

    st.caption(f"FieldNotes for Therapists · v{APP_VERSION} · Created by Nicole Chew-Helbig")


if __name__ == "__main__":
    main()
