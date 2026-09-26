# ============================================================
# Deterministic MCQ grading pipeline.
#
# Design (approved 2026-09-26, see conversation): an AI call is used ONLY
# ONCE PER EXAM to work out the correct answer for every multiple-choice
# question — never once per student. Every student's selected letter is
# then checked against that saved key with a plain equality comparison, in
# _score_mcq_answer below — never by asking an AI to judge it. This is what
# makes MCQ grading immune to the leniency/partial-credit failure mode
# documented across every vendor (see the "Best AI model for exam grading"
# research report): there is no judgment call left for an AI to be lenient
# about, only string equality.
#
# No faculty answer-key entry — the correct answer is worked out by Claude
# from the question paper alone, once, at question-paper-upload time.
# Anything Claude can't answer confidently is saved with confident=False
# rather than guessed, and surfaced to faculty as needing a look (never
# blocking grading — see determine_and_save_mcq_answer_keys).
# ============================================================

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.claude import generate_text
from app.utils.mcq_detect import detect_mcq_questions

logger = logging.getLogger("mcq_grading")

# Cost is a non-issue here (one call per exam, not per student — see module
# docstring), so this is chosen for accuracy, not price: per the
# user-approved research comparison, Sonnet 5 over 4.6 for lower
# hallucination/sycophancy; Opus 5.5 was considered and explicitly declined
# in favor of Sonnet 5 (2026-09-26).
MCQ_ANSWER_KEY_MODEL = "claude-sonnet-5"

# Marker the OCR prompt is told to emit for each MCQ question it can find in
# the student's script — deliberately distinctive so it can't collide with
# ordinary transcribed answer text.
MCQ_ANSWER_LINE_RE = re.compile(r"^MCQ_ANSWER\s+(\d+)\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


# ============================================================
# STEP A — determine the answer key (once per exam)
# ============================================================

_ANSWER_KEY_PROMPT_TEMPLATE = """You are an expert examiner determining the correct answer to \
multiple-choice questions from a question paper. For each question below, work out the single \
correct option letter.

If, and only if, a question is genuinely ambiguous, has no clearly correct option, or you are not \
at least highly confident, set "confident" to false rather than guessing — a wrong answer here \
would mark every student's answer to that question incorrectly, so do not guess.

QUESTIONS:
{questions_block}

Return ONLY a valid JSON object with exactly this structure (no markdown, no explanations, do NOT \
wrap in ```json or ``` blocks):

{{
  "answers": {{
    "<question_no>": {{"answer": "<A|B|C|D|...>", "confident": <true|false>, "reasoning": "<string, 1-2 sentences>"}}
  }}
}}"""


_OPTION_LETTERS = "ABCDEFGH"


def _letter_map(options: List[str]) -> Dict[str, str]:
    """Assigns A, B, C, ... to a question's option list, in order — the single
    source of truth for that mapping, used both when building the answer-key
    prompt below and when saving the key's options for the OCR step
    (determine_and_save_mcq_answer_keys) so the two can never drift apart."""
    return {
        (_OPTION_LETTERS[i] if i < len(_OPTION_LETTERS) else str(i + 1)): text
        for i, text in enumerate(options)
    }


def _build_questions_block(mcq_questions: Dict[int, List[str]]) -> str:
    lines = []
    for q_no in sorted(mcq_questions):
        lines.append(f"Question {q_no}:")
        for letter, option_text in _letter_map(mcq_questions[q_no]).items():
            lines.append(f"  {letter}) {option_text}")
        lines.append("")
    return "\n".join(lines)


def determine_mcq_answer_keys(
    mcq_questions: Dict[int, List[str]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """
    One Claude call for the WHOLE exam's MCQ questions at once (not one call per
    question) — still "once per exam, never per student" as approved, just batched
    for efficiency. Blocking — run via asyncio.to_thread().

    Returns (answer_key, token_usage) where answer_key is
    {"<question_no>": {"answer": "B", "confident": True, "reasoning": "..."}}.
    Never raises for a malformed model response — returns an empty answer_key so
    the caller can proceed with those questions unresolved (needs_review) instead
    of failing the whole question-paper upload.
    """
    if not mcq_questions:
        return {}, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    prompt = _ANSWER_KEY_PROMPT_TEMPLATE.format(questions_block=_build_questions_block(mcq_questions))

    try:
        text, usage = generate_text(prompt, model=MCQ_ANSWER_KEY_MODEL, max_tokens=4000)
    except Exception as e:
        logger.warning("determine_mcq_answer_keys: Claude call failed: %s", e)
        return {}, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    try:
        cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)
        parsed = json.loads(cleaned)
        answers = parsed.get("answers", {})
        if not isinstance(answers, dict):
            raise ValueError("'answers' is not an object")
    except Exception as e:
        logger.warning("determine_mcq_answer_keys: could not parse model response as JSON: %s", e)
        return {}, usage

    # Only keep entries for questions actually asked, with a valid shape —
    # never trust the model to have echoed back exactly the right set of keys.
    answer_key: Dict[str, Dict[str, Any]] = {}
    for q_no in mcq_questions:
        entry = answers.get(str(q_no))
        if not isinstance(entry, dict) or not entry.get("answer"):
            continue
        answer_key[str(q_no)] = {
            "answer": str(entry["answer"]).strip().upper(),
            "confident": bool(entry.get("confident", False)),
            "reasoning": str(entry.get("reasoning", ""))[:500],
        }

    return answer_key, usage


# ============================================================
# STEP B — parse each student's MCQ selections out of the OCR text
# ============================================================

def parse_mcq_answers_from_ocr_text(ocr_text: str) -> Dict[str, Optional[str]]:
    """Pulls "MCQ_ANSWER <n>: <letter|UNCLEAR>" lines the OCR prompt is asked to
    emit (see the mcq_question_numbers-aware branch of grading._OCR_PROMPT /
    extract_answer_text_with_gemini) out of the free-text OCR result.
    Returns {"<question_no>": "B"} or {"<question_no>": None} for UNCLEAR/unparsable —
    None must be treated as "could not read", never as "wrong", by the caller."""
    if not ocr_text:
        return {}

    result: Dict[str, Optional[str]] = {}
    for match in MCQ_ANSWER_LINE_RE.finditer(ocr_text):
        q_no, raw_value = match.group(1), match.group(2).strip()
        letter_match = re.match(r"^([A-Za-z])\b", raw_value)
        if raw_value.upper().startswith("UNCLEAR") or not letter_match:
            result[q_no] = None
        else:
            result[q_no] = letter_match.group(1).upper()
    return result


# ============================================================
# STEP C — deterministic, per-student scoring (no AI — plain comparison)
# ============================================================

def score_mcq_answer(
    question_no: int,
    student_letter: Optional[str],
    answer_key_entry: Optional[Dict[str, Any]],
    max_marks: float,
    cos: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Pure function, no AI/network call — this is the actual "deterministic code
    matching" step. Builds one questionwise_marking-shaped entry (matching the
    shape app/api/routers/grading.py already builds from Claude's output, so it
    merges into the same report/CO/results pipeline with no downstream changes).

    Three outcomes, never a partial/lenient score:
    - No usable answer key (Claude couldn't resolve it, or wasn't confident) ->
      flags.needs_review=True, 0 marks, clearly labelled — never silently guessed.
    - Student's letter unreadable (OCR said UNCLEAR / found nothing) ->
      flags.unanswered=True, 0 marks — same convention grading.py already uses
      for a skipped question in an optional group.
    - Both present -> exact letter match = full marks, anything else = 0. No
      partial credit is possible for an MCQ question by construction.
    """
    correct_letter = (answer_key_entry or {}).get("answer")
    key_confident = bool((answer_key_entry or {}).get("confident"))

    def _co_entries(all_or_nothing_marks: bool) -> List[Dict[str, Any]]:
        # Mirrors the AI-grading prompt's own rule ("cap each CO's awarded marks at
        # that CO's max marks"; "for unanswered questions, still emit a CO entry per
        # rubric CO with ai_marks=0") so CO/PO attainment keeps working identically
        # whether a question was graded by Claude or deterministically here — an MCQ
        # question is all-or-nothing, so a correct answer earns each CO's full max,
        # an incorrect/unscored one earns 0 for each, never a partial CO credit.
        entries = []
        for co in (cos or []):
            # Accepts either shape: the rubric's input cos ({"co_code","marks",
            # "description"}) or Claude's own output cos ({"co_code","ai_marks",
            # "remarks","max_marks"}) — apply_deterministic_mcq_overrides passes the
            # latter (it overrides an already-graded entry), so both key names must
            # resolve the same max-marks value.
            co_max = co.get("max_marks", co.get("marks", 0))
            entries.append({
                "co_code": co.get("co_code", ""),
                "ai_marks": co_max if all_or_nothing_marks else 0,
                "remarks": "Deterministic MCQ scoring.",
                "max_marks": co_max,
            })
        return entries

    if not correct_letter or not key_confident:
        return {
            "question_no": question_no, "max_marks": max_marks, "ai_awarded_marks": 0, "final_marks": 0,
            "parameters": [], "cos": _co_entries(all_or_nothing_marks=False),
            "reasoning": "No confidently-determined answer key is available for this question yet.",
            "feedback": "This question needs the answer key confirmed before it can be scored.",
            "improvement": "",
            "flags": {"incomplete": False, "irrelevant": False, "unanswered": False, "repetitive": False,
                      "needs_review": True},
        }

    if not student_letter:
        return {
            "question_no": question_no, "max_marks": max_marks, "ai_awarded_marks": 0, "final_marks": 0,
            "parameters": [], "cos": _co_entries(all_or_nothing_marks=False),
            "reasoning": "No selected option could be confidently read from the student's script.",
            "feedback": "No answer detected for this question.",
            "improvement": "",
            "flags": {"incomplete": False, "irrelevant": False, "unanswered": True, "repetitive": False,
                      "needs_review": False},
        }

    is_correct = student_letter.strip().upper() == correct_letter.strip().upper()
    marks = float(max_marks) if is_correct else 0.0
    return {
        "question_no": question_no, "max_marks": max_marks, "ai_awarded_marks": marks, "final_marks": marks,
        "parameters": [{
            "name": "Correct Option Selection", "weight_percentage": 100,
            "ai_score": marks, "remarks": (
                f"Student selected {student_letter}; correct answer is {correct_letter}."
                if is_correct else
                f"Student selected {student_letter}; correct answer is {correct_letter} — not a match."
            ),
        }],
        "cos": _co_entries(all_or_nothing_marks=is_correct),
        "reasoning": f"Deterministic match: selected={student_letter}, correct={correct_letter}.",
        "feedback": "Correct." if is_correct else f"Incorrect. The correct option was {correct_letter}.",
        "improvement": "" if is_correct else "Review this topic and the reasoning for the correct option.",
        "flags": {"incomplete": False, "irrelevant": False, "unanswered": False, "repetitive": False,
                  "needs_review": False},
    }


# ============================================================
# STEP D — merge deterministic MCQ results into Claude's questionwise_marking
# (safe-by-construction: Claude still grades the full rubric exactly as before,
# unchanged, zero risk to that well-tested path; this only OVERRIDES the specific
# question_no entries that have a confidently-resolved answer key, after the fact,
# with the deterministic result — so nothing about question numbering/ordering
# from grade_with_claude needs to change.)
# ============================================================

def apply_deterministic_mcq_overrides(
    questionwise_marking: List[Dict[str, Any]],
    mcq_answer_key: Dict[str, Dict[str, Any]],
    student_mcq_answers: Dict[str, Optional[str]],
) -> Tuple[List[Dict[str, Any]], int]:
    """Returns (new_questionwise_marking, override_count). Pure function, no AI/network
    call — testable with synthetic data. Leaves any question not in mcq_answer_key
    (subjective questions, or MCQ questions Claude never had a resolved key for)
    completely untouched — Claude's own grading of those stands as-is."""
    if not mcq_answer_key:
        return questionwise_marking, 0

    override_count = 0
    result = []
    for q in questionwise_marking:
        q_no = q.get("question_no")
        key_entry = mcq_answer_key.get(str(q_no))
        if key_entry is None:
            result.append(q)
            continue

        student_letter = student_mcq_answers.get(str(q_no))
        deterministic = score_mcq_answer(
            question_no=q_no,
            student_letter=student_letter,
            answer_key_entry=key_entry,
            max_marks=q.get("max_marks", 0),
            cos=q.get("cos"),
        )
        result.append(deterministic)
        override_count += 1

    return result, override_count


# ============================================================
# ORCHESTRATION — called once, from gemini.extract_and_patch_question_paper_text,
# right after the question-paper text itself is saved. Best-effort: any failure
# here must never affect the (already-successful) text extraction it follows.
# ============================================================

async def determine_and_save_mcq_answer_keys(
    db: AsyncIOMotorDatabase,
    folder_id: ObjectId,
    paper_text: str,
    faculty_id: str,
    user_id: Optional[str] = None,
) -> None:
    """No-op (and no AI call) when the paper has no detectable MCQ questions —
    a subjective-only exam never touches this pipeline or its cost."""
    try:
        mcq_questions = detect_mcq_questions(paper_text)
        if not mcq_questions:
            logger.info("[mcq-key] folder %s: no MCQ questions detected, skipping", folder_id)
            return

        logger.info("[mcq-key] folder %s: resolving answer key for %d MCQ question(s)",
                    folder_id, len(mcq_questions))
        answer_key, usage = await asyncio.to_thread(determine_mcq_answer_keys, mcq_questions)

        # Embed each question's option texts (letter -> text) into its saved key
        # entry — the OCR step needs these to recognize a student who wrote out
        # the answer's wording instead of a bare letter (see
        # _build_option_letter_map / the mcq_options param of
        # extract_answer_text_with_gemini in grading.py). Without this, OCR only
        # knows the question NUMBER is multiple-choice, not what A/B/C/D mean for
        # it, and has no way to match written-out text back to a letter.
        for q_no, options in mcq_questions.items():
            if str(q_no) in answer_key:
                answer_key[str(q_no)]["options"] = _letter_map(options)

        needs_review_count = sum(
            1 for q_no in mcq_questions
            if not (answer_key.get(str(q_no)) or {}).get("confident")
        )

        now = datetime.now(timezone.utc)
        await db["newsavedDocs"].update_one(
            {"_id": folder_id},
            {"$set": {
                "mcq_answer_key": answer_key,
                "mcq_answer_key_at": now,
                "mcq_answer_key_needs_review_count": needs_review_count,
                "updated_at": now,
            }},
        )
        logger.info("[mcq-key] folder %s: saved %d answer(s), %d need review",
                    folder_id, len(answer_key), needs_review_count)

        if user_id and (usage.get("input_tokens") or usage.get("output_tokens")):
            from app.models.ai_usage_event import Feature, Provider
            from app.services.ai_usage import record_ai_usage
            from app.utils.token_usage import resolve_institute_id_for_faculty

            institute_id = await resolve_institute_id_for_faculty(db, faculty_id)
            await record_ai_usage(
                db, user_id=user_id, provider=Provider.CLAUDE, model=MCQ_ANSWER_KEY_MODEL,
                feature=Feature.GRADING_MCQ_ANSWER_KEY, usage=usage,
                institute_id=str(institute_id) if institute_id else None,
            )

    except Exception as e:
        # Best-effort — matches every other post-extraction step in this codebase
        # (record_ai_usage, save_grading_tokens_to_institute, etc.). A failure here
        # must never surface as a question-paper-upload failure; the exam simply
        # keeps no mcq_answer_key, and every question is graded by Claude as before.
        logger.warning("[mcq-key] folder %s: failed (non-fatal): %s", folder_id, e)
