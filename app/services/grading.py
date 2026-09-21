# ============================================================
# AI answer-script grading — ported from the QuizGradingAssistant class
# embedded in Flask's server.py (lines 218-981).
#
# extract_answer_text_with_gemini and grade_with_claude /
# generate_transcript_html_with_claude are blocking (sync SDK calls) — run
# via asyncio.to_thread() from async callers, same convention as every
# other service module in this project.
# ============================================================

import ast
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from app.services.claude import generate_text
from app.services.gemini import generate_content_from_file

CLAUDE_GRADING_MODEL = "claude-sonnet-4-6"
GEMINI_OCR_MODEL = "gemini-3.1-pro-preview"


def _empty_claude_tokens(call_name: str) -> Dict[str, Any]:
    return {"call": call_name, "model": CLAUDE_GRADING_MODEL, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def _empty_gemini_tokens(call_name: str) -> Dict[str, Any]:
    return {"call": call_name, "model": GEMINI_OCR_MODEL, "prompt_tokens": 0, "candidate_tokens": 0, "total_tokens": 0}


def _strip_fences(text: str) -> str:
    text = re.sub(r"^```html\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


# ============================================================
# STEP 1 — OCR the student's answer script (Gemini)
# ============================================================

_OCR_PROMPT = (
    "Extract ALL text from this student's handwritten/typed answer script PDF with high accuracy. "
    "Preserve question numbers, sub-question labels (a/b/c), and the structure of each answer as "
    "written, including diagrams or figures described as [Diagram: ...] where present. "
    "OCR of handwriting may be imperfect — transcribe as faithfully as possible without correcting "
    "the student's own mistakes.\n"
    "Return plain text only. Do not use markdown."
)


def extract_answer_text_with_gemini(pdf_bytes: bytes) -> Tuple[str, Dict[str, Any]]:
    """Blocking — run via asyncio.to_thread()."""
    try:
        text, usage = generate_content_from_file(pdf_bytes, "application/pdf", _OCR_PROMPT, model=GEMINI_OCR_MODEL)
    except Exception as e:
        raise RuntimeError(f"Answer script OCR failed: {e}") from e

    return text, {
        "call": "extract_text_with_gemini",
        "model": GEMINI_OCR_MODEL,
        "prompt_tokens": usage["prompt_tokens"],
        "candidate_tokens": usage["candidate_tokens"],
        "total_tokens": usage["total_tokens"],
    }


# ============================================================
# STEP 2 — Grade with Claude
# ============================================================

def build_grading_rubric(evaluation_rules: List[Dict[str, Any]]) -> str:
    lines = []
    for idx, rule in enumerate(evaluation_rules, 1):
        lines.append(f"Question {idx}:")
        lines.append(f"- Marks Range: {rule.get('minMarks', 0)} to {rule.get('maxMarks', 0)}")

        guidelines = rule.get("guidelines")
        if guidelines:
            lines.append(f"- Guidelines: {guidelines}")

        parameters = rule.get("parameters")
        if parameters:
            lines.append("- Evaluation Parameters:")
            for p in parameters:
                lines.append(f"  • {p.get('name', '')} — {p.get('percentage', 0)}% weight")

        cos = rule.get("cos")
        if cos:
            lines.append("- Course Outcomes (COs) to assess:")
            for co in cos:
                lines.append(
                    f"  • {co.get('co_code', '')} (max marks for this CO: {co.get('marks', 0)}): "
                    f"{co.get('description', '')}"
                )
        else:
            lines.append("- Course Outcomes: None defined for this question")

    return "\n".join(lines)


_GRADING_PROMPT_TEMPLATE = """You are an expert academic examiner grading a student's handwritten answer \
script that has been OCR-extracted from a scanned PDF. OCR may introduce minor errors (misread digits, \
stray characters) — do not penalize the student for these if the underlying method or final value is \
clearly close to correct.

QUESTION PAPER:
{question_text}

STUDENT ANSWER:
{answer_text}

EVALUATION RUBRIC:
{rubric}

SECTION 1 — CHOICE-BASED QUESTION DETECTION
Some questions may be optional (e.g. "Attempt any 3 of 5"). Detect such groups. For questions in an \
optional group the student did not attempt, mark them unanswered: "flags.unanswered"=true, \
"ai_awarded_marks"=0, "final_marks"=0. For attempted questions, grade normally. Every rubric question \
must appear in your output, whether attempted or not.

SECTION 2 — GRADING RULES
- Award marks only for content actually present in the student's answer — never invent or assume content.
- Give proportional/partial credit for partially correct answers.
- Penalize missing steps, diagrams, or derivations where the rubric calls for them.
- If a question was attempted, award at least some minimum marks reflecting the attempt.
- Each parameter's awarded score must not exceed its weight_percentage share of the question's max marks.

SECTION 3 — COURSE OUTCOME (CO) GRADING RULES
- Evaluate the answer against each CO description listed in the rubric for that question.
- Cap each CO's awarded marks at that CO's max marks as defined in the rubric.
- For unanswered questions, still emit a CO entry per rubric CO with ai_marks=0 and the rubric's max_marks.
- If a question has no COs defined in the rubric, return an empty "cos": [] for it.
- Never invent CO codes or marks not present in the rubric.

SECTION 4 — FEEDBACK QUALITY
- Every question (answered or not) needs detailed "reasoning", "feedback", and "improvement" text — \
minimum 3-4 sentences each.
- Each parameter's "remarks" must be descriptive, not a one-word judgement.
- Explicitly reference how marks were allocated per parameter and against the rubric's criteria.

SECTION 5 — OUTPUT RULES
- Every rubric question must appear in "questionwise_marking", in order.
- Unanswered questions still get full-schema entries with zeroed marks as described above.
- Include accurate "questions_attempted" and "questions_total" counts in "summary".

Return ONLY a valid JSON object with exactly this structure (no markdown, no explanations, do NOT wrap \
in ```json or ``` blocks):

{{
  "summary": {{
    "total_ai_marks": <number>,
    "total_max_marks": <number>,
    "questions_attempted": <number>,
    "questions_total": <number>
  }},
  "questionwise_marking": [
    {{
      "question_no": <number>,
      "max_marks": <number>,
      "ai_awarded_marks": <number>,
      "final_marks": <number>,
      "parameters": [
        {{"name": "<string>", "weight_percentage": <number>, "ai_score": <number>, "remarks": "<string>"}}
      ],
      "cos": [
        {{"co_code": "<string>", "ai_marks": <number>, "remarks": "<string>", "max_marks": <number>}}
      ],
      "reasoning": "<string>",
      "feedback": "<string>",
      "improvement": "<string>",
      "flags": {{"incomplete": <bool>, "irrelevant": <bool>, "unanswered": <bool>, "repetitive": <bool>}}
    }}
  ]
}}"""


def grade_with_claude(question_text: str, answer_text: str, evaluation_rules: List[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    """
    Blocking — run via asyncio.to_thread().
    Never raises — matches Flask's behavior of returning an error string + empty
    token usage on failure, letting the caller's JSON parsing surface the problem.
    """
    rubric = build_grading_rubric(evaluation_rules)
    prompt = _GRADING_PROMPT_TEMPLATE.format(question_text=question_text, answer_text=answer_text, rubric=rubric)

    try:
        text, usage = generate_text(prompt, model=CLAUDE_GRADING_MODEL, max_tokens=20000)
        return text, {
            "call": "grade_with_claude", "model": CLAUDE_GRADING_MODEL,
            "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
            "total_tokens": usage["total_tokens"],
        }
    except Exception as e:
        return f"Claude evaluation error: {e}", _empty_claude_tokens("grade_with_claude")


# ============================================================
# STEP 3 — Transcript HTML (Claude, with a no-LLM fallback)
# ============================================================

_TRANSCRIPT_PROMPT_TEMPLATE = """You are producing a clean academic transcript of a student's answer \
script for record-keeping. Reformat the OCR'd answer text below into a complete, standalone HTML \
document — legible, well-structured, one section per question, no grading or commentary, just a \
faithful clean transcription.

STUDENT: {student_name}

RAW OCR'D ANSWER TEXT:
{answer_text}

STYLING: simple, print-friendly, Arial/sans-serif, a header with the student's name and "Answer \
Transcript", one block per question.

Return ONLY the complete HTML document. No markdown fences, no explanations. Start with <!DOCTYPE html> \
and end with </html>."""


def generate_transcript_html_with_claude(answer_text: str, student_name: str = "Student") -> Tuple[str, Dict[str, Any]]:
    """Blocking — run via asyncio.to_thread(). Falls back to a non-LLM formatter on failure."""
    prompt = _TRANSCRIPT_PROMPT_TEMPLATE.format(student_name=student_name, answer_text=answer_text)
    try:
        html, usage = generate_text(prompt, model=CLAUDE_GRADING_MODEL, max_tokens=18000)
        html = _strip_fences(html)
        return html, {
            "call": "generate_transcript_html_with_claude", "model": CLAUDE_GRADING_MODEL,
            "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
            "total_tokens": usage["total_tokens"],
        }
    except Exception as e:
        logging.warning("Transcript generation via Claude failed, using basic fallback: %s", e)
        return generate_basic_transcript_html(answer_text, student_name), _empty_claude_tokens(
            "generate_transcript_html_with_claude"
        )


def _escape_html(text: Any) -> str:
    if text is None:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def generate_basic_transcript_html(transcript_text: str, student_name: str = "Student") -> str:
    """Pure-Python fallback formatter — no LLM call."""
    body = _escape_html(transcript_text).replace("\n", "<br>\n")
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: Arial, sans-serif; color: #111827; line-height: 1.6; margin: 24px; }}
  h1 {{ color: #1d4ed8; font-size: 18px; }}
  .meta {{ color: #6b7280; font-size: 12px; margin-bottom: 16px; }}
</style>
</head>
<body>
<h1>Answer Transcript</h1>
<div class="meta">Student: {_escape_html(student_name)}</div>
<div>{body}</div>
</body>
</html>"""


# ============================================================
# STEP 4 — Evaluation report HTML (pure Python, no LLM)
# ============================================================

def _grade_letter(percentage: float) -> str:
    if percentage >= 90:
        return "A+"
    if percentage >= 80:
        return "A"
    if percentage >= 70:
        return "B+"
    if percentage >= 60:
        return "B"
    if percentage >= 50:
        return "C"
    if percentage >= 40:
        return "D"
    return "F"


def _status_label(flags: Dict[str, Any]) -> tuple:
    """Single priority-ordered status for the summary table + its pill color
    (matching the original Flask-era report: unanswered beats incomplete
    beats irrelevant beats a clean "Evaluated")."""
    if flags.get("unanswered"):
        return "Not Attempted", "#dc2626", "#fee2e2"
    if flags.get("incomplete"):
        return "Incomplete", "#b45309", "#fef3c7"
    if flags.get("irrelevant"):
        return "Irrelevant", "#6b7280", "#f3f4f6"
    return "Evaluated", "#15803d", "#dcfce7"


def _score_bar_color(percentage: float) -> str:
    if percentage < 25:
        return "#ef4444"
    if percentage < 50:
        return "#f97316"
    if percentage < 75:
        return "#3b82f6"
    return "#22c55e"


def generate_evaluation_report_html(
    grading_json: Dict[str, Any], student_name: str = "Student", total_max_marks: Optional[float] = None
) -> str:
    summary = grading_json.get("summary", {}) or {}
    questions = grading_json.get("questionwise_marking", []) or []

    total_ai_marks = summary.get("total_ai_marks", sum(q.get("ai_awarded_marks", 0) for q in questions))
    max_marks = total_max_marks if total_max_marks is not None else summary.get("total_max_marks", 0)
    percentage = round((total_ai_marks / max_marks) * 100, 2) if max_marks else 0
    grade = _grade_letter(percentage)
    attempted = summary.get("questions_attempted", "-")
    total_q = summary.get("questions_total", len(questions))

    # ── Question-wise Summary table ──────────────────────────────────────
    summary_rows = []
    for q in questions:
        flags = q.get("flags", {}) or {}
        status_text, status_fg, status_bg = _status_label(flags)
        summary_rows.append(f"""
<tr>
  <td>{q.get('question_no', '')}</td>
  <td>{q.get('max_marks', 0)}</td>
  <td><strong>{q.get('ai_awarded_marks', 0)}</strong></td>
  <td><span class="status-pill" style="color:{status_fg};background:{status_bg};">{status_text}</span></td>
</tr>""")

    # ── Detailed per-question analysis ───────────────────────────────────
    question_blocks = []
    for q in questions:
        flags = q.get("flags", {}) or {}
        flag_badges = "".join(
            f'<span class="badge">{icon} {name}</span>'
            for name, icon, on in [
                ("Not Attempted", "&#10060;", flags.get("unanswered")),
                ("Incomplete", "&#9888;", flags.get("incomplete")),
                ("Irrelevant", "&#8960;", flags.get("irrelevant")),
                ("Repetitive", "&#8635;", flags.get("repetitive")),
            ]
            if on
        )

        q_marks = q.get("ai_awarded_marks", 0)
        q_max = q.get("max_marks", 0) or 1
        q_pct = round((q_marks / q_max) * 100, 1) if q_max else 0
        bar_color = _score_bar_color(q_pct)

        co_rows = "".join(
            f"<tr><td><span class=\"co-chip\">{_escape_html(c.get('co_code'))}</span></td>"
            f"<td>{c.get('ai_marks', 0)}</td><td>{_escape_html(c.get('remarks'))}</td></tr>"
            for c in (q.get("cos") or [])
        )
        param_rows = "".join(
            f"<tr><td>{_escape_html(p.get('name'))}</td><td>{p.get('weight_percentage', 0)}%</td>"
            f"<td>{p.get('ai_score', 0)}</td><td>{_escape_html(p.get('remarks'))}</td></tr>"
            for p in (q.get("parameters") or [])
        )

        question_blocks.append(f"""
<div class="question">
  <div class="q-title">Question {q.get('question_no', '')}</div>
  <div class="q-badges">{flag_badges}</div>
  <div class="score-bar" style="background:{bar_color};">{q_marks} / {q_max} <span class="pct">({q_pct}%)</span></div>
  {f'<div class="section-label">&#128218; CO-wise Marks</div><table class="co-table"><tr><th>CO Code</th><th>Marks Awarded</th><th>Remarks</th></tr>{co_rows}</table>' if co_rows else ''}
  {f'<div class="section-label">&#128202; Parameter-wise Breakdown</div><table class="param-table"><tr><th>Parameter</th><th>Weight</th><th>Score</th><th>Remarks</th></tr>{param_rows}</table>' if param_rows else ''}
  <div class="callout callout-green"><div class="callout-label">&#128269; Reasoning</div>{_escape_html(q.get('reasoning'))}</div>
  <div class="callout callout-blue"><div class="callout-label">&#128172; Feedback</div>{_escape_html(q.get('feedback'))}</div>
  <div class="callout callout-orange"><div class="callout-label">&#127919; How to Improve</div>{_escape_html(q.get('improvement'))}</div>
</div>""")

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: Arial, sans-serif; color: #111827; margin: 24px; }}
  .banner {{ background: #22c55e; border-radius: 8px; padding: 20px 24px; margin-bottom: 20px; }}
  .banner h1 {{ color: #ffffff; font-size: 24px; margin: 0 0 6px 0; }}
  .banner .meta {{ color: #ecfdf5; font-size: 12px; }}
  .top-stats {{ display: flex; align-items: center; gap: 24px; margin-bottom: 24px; }}
  .grade-circle {{ width: 64px; height: 64px; border-radius: 50%; background: #3b82f6; color: #fff;
                    display: flex; align-items: center; justify-content: center; font-size: 26px; font-weight: bold; flex-shrink: 0; }}
  .stat {{ }}
  .stat .label {{ font-size: 10px; color: #64748b; text-transform: uppercase; font-weight: bold; }}
  .stat .value {{ font-size: 16px; font-weight: bold; color: #111827; }}
  .summary-title {{ font-size: 14px; font-weight: bold; margin: 20px 0 8px; text-align: center; }}
  table {{ width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 11px; }}
  table th, table td {{ border: 1px solid #e2e8f0; padding: 6px 8px; text-align: left; }}
  .summary-table th {{ background: #1f2937; color: #fff; text-align: center; }}
  .summary-table td {{ text-align: center; }}
  .status-pill {{ font-size: 10px; padding: 2px 8px; border-radius: 10px; font-weight: bold; }}
  .section-heading {{ font-size: 15px; font-weight: bold; margin: 24px 0 4px; padding-bottom: 6px; border-bottom: 2px solid #22c55e; }}
  .question {{ border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px 16px; margin-bottom: 16px; }}
  .q-title {{ font-weight: bold; font-size: 14px; margin-bottom: 4px; }}
  .q-badges {{ margin-bottom: 8px; }}
  .badge {{ display: inline-block; background: #f3f4f6; color: #4b5563; font-size: 10px; padding: 2px 8px; border-radius: 10px; margin-right: 4px; }}
  .score-bar {{ color: #fff; font-weight: bold; text-align: center; border-radius: 6px; padding: 10px; margin: 8px 0; font-size: 14px; }}
  .score-bar .pct {{ font-weight: normal; font-size: 11px; }}
  .section-label {{ font-size: 11px; font-weight: bold; color: #374151; margin: 10px 0 4px; }}
  .co-table th {{ background: #ea580c; color: #fff; }}
  .param-table th {{ background: #1f2937; color: #fff; }}
  .co-chip {{ background: #ffedd5; color: #c2410c; font-weight: bold; font-size: 10px; padding: 2px 6px; border-radius: 4px; }}
  .callout {{ border-radius: 6px; padding: 8px 12px; margin-top: 8px; font-size: 11px; border-left: 4px solid; }}
  .callout-label {{ font-weight: bold; margin-bottom: 4px; }}
  .callout-green {{ background: #f0fdf4; border-color: #22c55e; }}
  .callout-blue {{ background: #eff6ff; border-color: #3b82f6; }}
  .callout-orange {{ background: #fff7ed; border-color: #f97316; }}
  .footer {{ text-align: center; color: #9ca3af; font-size: 10px; margin-top: 28px; }}
</style>
</head>
<body>
  <div class="banner">
    <h1>EVALUATION REPORT</h1>
    <div class="meta"><strong>Student:</strong> {_escape_html(student_name)}</div>
  </div>

  <div class="top-stats">
    <div class="grade-circle">{grade}</div>
    <div class="stat"><div class="label">Score</div><div class="value">{total_ai_marks} / {max_marks}</div></div>
    <div class="stat"><div class="label">Percentage</div><div class="value">{percentage}%</div></div>
    <div class="stat"><div class="label">Attempted</div><div class="value">{attempted} / {total_q}</div></div>
  </div>

  <div class="summary-title">Question-wise Summary</div>
  <table class="summary-table">
    <tr><th>Q. No.</th><th>Max Marks</th><th>Marks Obtained</th><th>Status</th></tr>
    {"".join(summary_rows)}
  </table>

  <div class="section-heading">Detailed Question-wise Analysis</div>
  {"".join(question_blocks)}

  <div class="footer">
    <div>Note: This is an AI-generated evaluation.</div>
    <div>Generated by Gradelytics | &copy; 2025</div>
  </div>
</body>
</html>"""


# ============================================================
# safe_json_parse — 10-stage repair pipeline for LLM JSON output
# ============================================================

def safe_json_parse(text: str) -> Dict[str, Any]:
    if not text or not text.strip():
        raise ValueError("Empty response from AI model")

    cleaned = text.strip()

    # 1. strip ```json / ``` fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()

    # 2. slice from first { to last }
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found in AI response")
    cleaned = cleaned[start:end + 1]

    # 3. normalize smart quotes
    cleaned = (
        cleaned.replace("“", '"').replace("”", '"')
        .replace("‘", "'").replace("’", "'")
    )

    # 4. strip trailing commas before } or ]
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)

    # 5. strip stray control characters (keep \n and \t)
    cleaned = "".join(c for c in cleaned if c in "\n\t" or ord(c) >= 32)

    # 6. try direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 7. fallback: naive single -> double quote conversion (skip escaped quotes)
    try:
        naive = re.sub(r"(?<!\\)'", '"', cleaned)
        naive = re.sub(r",\s*([}\]])", r"\1", naive)
        return json.loads(naive)
    except json.JSONDecodeError:
        pass

    # 8. fallback: ast.literal_eval round-trip (handles Python-style dict literals)
    try:
        parsed = ast.literal_eval(cleaned)
        return json.loads(json.dumps(parsed))
    except Exception:
        pass

    # 9. give up — log a bounded snippet (never write the full raw model
    #    output to a file on disk: unbounded, and this path is reachable by
    #    an unauthenticated-ish caller triggering a grading job).
    logging.warning("safe_json_parse: unparseable AI response (first 500 chars): %s", cleaned[:500])
    raise ValueError("Could not parse the AI model's response as JSON.")
