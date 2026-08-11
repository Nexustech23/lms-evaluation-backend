# ============================================================
# AI TUTOR "V1" — EXTRACTION / CLAUDE / PDF HELPERS
# Ported from services/gemini_service.py + services/claude_service.py +
# services/pdf_service.py (the trio ai_tutor_controller.py — the unused
# "v1" implementation — depends on).
#
# NOTE: despite the name, extract_text_with_gemini() never calls Gemini —
# it's local pdfplumber/PyPDF2 extraction, PDF-only (raises on any other
# extension despite the route accepting png/jpg/jpeg too), and returns a
# hardcoded MOCK token_usage (120/300/420). Ported verbatim, including
# this quirk — not "fixed" to actually call Gemini, since that would
# change billing/behavior beyond what was asked.
#
# All functions here are blocking (file I/O, Claude SDK, ReportLab) — run
# via asyncio.to_thread() from the router.
# ============================================================

import logging
import os
import re
from typing import Any, Dict, Optional

import anthropic
import markdown2
import pdfplumber
from PyPDF2 import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from reportlab.platypus.tables import Table, TableStyle

from app.core.config import settings

logger = logging.getLogger(__name__)

UPLOAD_FOLDER = "uploads/homework"
PDF_FOLDER = "uploads/generated_pdfs"
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}
MAX_FILE_SIZE = 10 * 1024 * 1024

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PDF_FOLDER, exist_ok=True)

_anthropic_client: Optional[anthropic.Anthropic] = None


def _get_anthropic() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _anthropic_client


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def secure_filename(filename: str) -> str:
    """Minimal werkzeug.secure_filename equivalent — strips path separators and unsafe chars."""
    filename = os.path.basename(filename.replace("\\", "/"))
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", filename)
    return filename or "file"


# ============================================================
# TEXT EXTRACTION (local pdfplumber → PyPDF2 fallback; PDF only)
# ============================================================

def extract_pdf_text(file_path: str) -> str:
    extracted_text = ""

    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    extracted_text += page_text + "\n"
        if extracted_text.strip():
            logger.info("[PDF] Extracted using pdfplumber.")
            return extracted_text
    except Exception as e:
        logger.error("[PDF] pdfplumber failed: %s", e)

    try:
        reader = PdfReader(file_path)
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                extracted_text += page_text + "\n"
        if extracted_text.strip():
            logger.info("[PDF] Extracted using PyPDF2.")
            return extracted_text
    except Exception as e:
        logger.error("[PDF] PyPDF2 failed: %s", e)

    raise ValueError("Could not extract readable text from PDF. Please upload a clear text-based PDF.")


def extract_text_with_gemini(file_path: str) -> Dict[str, Any]:
    """Despite the name, this is local extraction only — see module docstring."""
    logger.info("[Gemini] Extracting text from: %s", file_path)

    if not os.path.exists(file_path):
        raise ValueError("Uploaded file not found.")

    file_extension = file_path.split(".")[-1].lower()

    if file_extension == "pdf":
        extracted_text = extract_pdf_text(file_path)
    else:
        raise ValueError("Unsupported file type.")

    if not extracted_text.strip():
        raise ValueError("Uploaded PDF appears empty or unreadable.")

    logger.info("[Gemini] Extraction successful.")

    token_usage = {"prompt_tokens": 120, "completion_tokens": 300, "total_tokens": 420}
    return {"extracted_text": extracted_text, "token_usage": token_usage}


# ============================================================
# CLAUDE GENERATION
# ============================================================

def generate_homework_with_claude(prompt: str, extracted_text: str, homework_type: str, response_style: str) -> Dict[str, Any]:
    logger.info("[Claude] Generating homework solution.")
    extracted_text = extracted_text[:15000]

    final_prompt = f"""
You are an advanced AI Homework Assistant.

Your task is to generate a high-quality,
well-structured, student-friendly homework solution.

━━━━━━━━━━━━━━━━━━
HOMEWORK DETAILS
━━━━━━━━━━━━━━━━━━

Homework Type:
{homework_type}

Response Style:
{response_style}

Student Prompt:
{prompt}

━━━━━━━━━━━━━━━━━━
EXTRACTED STUDY MATERIAL
━━━━━━━━━━━━━━━━━━

{extracted_text}

━━━━━━━━━━━━━━━━━━
INSTRUCTIONS
━━━━━━━━━━━━━━━━━━

1. Generate a clean and professional solution.

2. Use proper headings and subheadings.

3. Explain concepts in simple language.

4. Use bullet points where needed.

5. Add examples if relevant.

6. If code is required:
   - Use properly formatted code blocks
   - Add comments in code

7. If mathematical concepts appear:
   - Explain formulas step-by-step

8. Keep the response highly readable.

9. Avoid repeating the same information.

10. Make the output look like:
    professional AI-generated homework notes.

━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━

Use markdown formatting:

# Main Heading

## Sub Heading

- Bullet Points

━━━━━━━━━━━━━━━━━━
IMPORTANT
━━━━━━━━━━━━━━━━━━

- Do not mention AI limitations.
- Do not say "As an AI model".
- Do not generate fake information.
- Use the extracted material as the primary source.
"""

    response = _get_anthropic().messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        temperature=0.4,
        messages=[{"role": "user", "content": final_prompt}],
    )

    generated_content = response.content[0].text
    if not generated_content.strip():
        raise ValueError("Claude returned empty response.")

    logger.info("[Claude] Homework generation successful.")
    return {
        "generated_content": generated_content,
        "token_usage": {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens},
    }


def generate_notes_with_claude(prompt: str, extracted_text: str, notes_type: str, notes_length: str) -> Dict[str, Any]:
    logger.info("[Claude] Generating AI notes.")
    extracted_text = extracted_text[:15000]

    word_limit_map = {"1 Page": 500, "3 Pages": 1500, "5 Pages": 2500}
    target_words = word_limit_map.get(notes_length, 1500)

    final_prompt = f"""
You are an advanced AI Notes Generator.

Generate structured, smart,
student-friendly study notes.

━━━━━━━━━━━━━━━━━━
NOTES DETAILS
━━━━━━━━━━━━━━━━━━

Notes Type:
{notes_type}

Notes Length:
{notes_length}

Target Word Count:
Approximately {target_words} words

Student Prompt:
{prompt}

━━━━━━━━━━━━━━━━━━
EXTRACTED STUDY MATERIAL
━━━━━━━━━━━━━━━━━━

{extracted_text}

━━━━━━━━━━━━━━━━━━
INSTRUCTIONS
━━━━━━━━━━━━━━━━━━

1. Use proper headings and subheadings.

2. Use bullet points where needed.

3. Explain concepts clearly and simply.

4. Highlight important points.

5. Add examples if relevant.

6. Keep the response concise.

7. Avoid unnecessary repetition.

8. Make the notes highly readable.

9. Keep notes aligned to:
   approximately {target_words} words.

10. Make the output look like:
    professional AI-generated notes.

━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━

Use markdown formatting:

# Main Heading

## Sub Heading

- Bullet Points

━━━━━━━━━━━━━━━━━━
IMPORTANT
━━━━━━━━━━━━━━━━━━

- Do not mention AI limitations.
- Do not say "As an AI model".
- Do not generate fake information.
- Use the extracted material as the primary source.
"""

    response = _get_anthropic().messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2500,
        temperature=0.4,
        messages=[{"role": "user", "content": final_prompt}],
    )

    generated_content = response.content[0].text
    if not generated_content.strip():
        raise ValueError("Claude returned empty notes response.")

    logger.info("[Claude] Notes generation successful.")
    return {
        "generated_content": generated_content,
        "token_usage": {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens},
    }


# ============================================================
# PDF GENERATION (ReportLab, saved to local uploads/generated_pdfs/)
# ============================================================

def _clean_ai_content(content: str) -> str:
    clean_content = re.sub(r"[^\x00-\x7F]+", "", content)
    return clean_content.replace("\n", "<br/>")


def _build_pdf(title: str, footer: str, details_data: list, generated_content: str, pdf_filename: str) -> Dict[str, str]:
    pdf_path = os.path.join(PDF_FOLDER, pdf_filename)

    doc = SimpleDocTemplate(pdf_path, pagesize=A4, rightMargin=40, leftMargin=40, topMargin=50, bottomMargin=40)
    styles = getSampleStyleSheet()
    story = [Paragraph(title, styles["Title"]), Spacer(1, 25)]

    details_table = Table(details_data, colWidths=[150, 320])
    details_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#6C63FF")),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("BACKGROUND", (1, 0), (1, -1), colors.whitesmoke),
        ("GRID", (0, 0), (-1, -1), 1, colors.lightgrey),
    ]))
    story.append(details_table)
    story.append(Spacer(1, 30))

    story.append(Paragraph("Generated Content", styles["Heading2"]))
    story.append(Spacer(1, 15))

    markdown_html = markdown2.markdown(generated_content)
    formatted_content = _clean_ai_content(markdown_html)
    story.append(Paragraph(formatted_content, styles["BodyText"]))
    story.append(Spacer(1, 25))

    story.append(Paragraph(footer, styles["Italic"]))

    doc.build(story)

    return {"pdf_path": f"{PDF_FOLDER}/{pdf_filename}", "pdf_filename": pdf_filename}


def generate_homework_pdf(doc_id: str, prompt: str, homework_type: str, response_style: str, generated_content: str) -> Dict[str, str]:
    return _build_pdf(
        title="AI Homework Solution",
        footer="Generated using AI Homework Assistant",
        details_data=[["Homework Type", homework_type], ["Response Style", response_style], ["Student Prompt", prompt]],
        generated_content=generated_content,
        pdf_filename=f"homework_{doc_id}.pdf",
    )


def generate_notes_pdf(doc_id: str, prompt: str, notes_type: str, notes_length: str, generated_content: str) -> Dict[str, str]:
    return _build_pdf(
        title="AI Generated Notes",
        footer="Generated using AI Notes Generator",
        details_data=[["Notes Type", notes_type], ["Notes Length", notes_length], ["Student Prompt", prompt]],
        generated_content=generated_content,
        pdf_filename=f"notes_{doc_id}.pdf",
    )
