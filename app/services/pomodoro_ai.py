# ============================================================
# POMODORO AI HELPERS
# Ported from controllers/self_learner/pomodoro_controller.py's `_gemini_*`
# functions — all four actually call Claude in the Flask original (the
# "_gemini_" naming there is a leftover from an earlier Gemini-based
# implementation that was swapped to Claude without renaming). Named
# accurately here.
#
# Gemini is used only for OCR/vision (uploaded-document text extraction,
# handwritten-answer extraction) — this reuses app.services.gemini's
# generate_content_from_file(), the same shared helper
# app/api/routers/ai_tutor.py already uses for equivalent file-extraction,
# instead of re-implementing Flask's raw Gemini Files API upload call
# (client.files.upload(...)). Functionally equivalent (inline-bytes vision
# call vs. upload-then-reference) and comes with retry/backoff for free —
# an implementation simplification, not a behavior change, so it's not one
# of the "known Flask bugs" ported verbatim elsewhere in this feature.
#
# All functions here are blocking SDK calls — run via asyncio.to_thread()
# from the router.
# ============================================================

import json
import mimetypes
import re
from typing import Any, Dict, List, Tuple

from bson import ObjectId

from app.services.claude import generate_text
from app.services.gemini import generate_content_from_file

_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 8192


def _parse_json(raw: str) -> Any:
    """Strip markdown code fences Anthropic sometimes wraps around JSON, then parse."""
    raw = raw.strip()
    raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
    raw = re.sub(r"\s*```\s*$", "", raw)
    return json.loads(raw.strip())


def generate_notes(prompt: str, config: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """AI-driven mode: generate `num_tests` study sections from a topic prompt."""
    num_sections = config["num_tests"]
    total_study_mins = config["total_study_time_mins"]
    per_section_mins = max(5, total_study_mins // num_sections)

    prompt_content = f"""
        You are an expert study content creator. A student wants to study the following topic:

        TOPIC: {prompt}

        Generate structured study notes divided into Exactly {num_sections} sections.
        Total study time available: {total_study_mins} minutes ({per_section_mins} minutes per section).

        Return ONLY a valid JSON array (no markdown fences, no explanations) with this exact structure:
        [
            {{
                "title": "Section 1: <descriptive section title>",
                "content": "<comprehensive study notes in markdown format, min 300 words per section>",
                "study_duration_mins": {per_section_mins}
            }},
            ...
        ]

        Rules:
        - Exactly {num_sections} sections
        - Each section must be self-contained and build on the previous
        - Use markdown: headers, bullet points, bold key terms
        - Content must be educational, accurate, and student-friendly
        - Return ONLY the JSON array
    """

    text, usage = generate_text(prompt_content, model=_MODEL, max_tokens=_MAX_TOKENS)
    return _parse_json(text), usage


def extract_and_section(text: str, config: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """AI-assisted mode: reorganise the student's own uploaded notes into `num_tests` sections."""
    num_sections = config["num_tests"]
    total_study_mins = config["total_study_time_mins"]
    per_section_mins = max(5, total_study_mins // num_sections)

    prompt_content = f"""
        You are an expert study content organiser. A student has uploaded their own notes.

        UPLOADED NOTES: {text}

        Organise and divide these notes into EXACTLY {num_sections} logical sections for a structured study session.
        Total study time: {total_study_mins} minutes ({per_section_mins} minutes per section).

        Return ONLY a valid JSON array (no markdown fences) with this structure:
        [
            {{
                "title": "Section 1: <descriptive title based on content>",
                "content": "<the relevant portion of the notes, cleaned and formatted in markdown>",
                "study_duration_mins": {per_section_mins}
            }},
            ...
        ]

        Rules:
        - Exactly {num_sections} sections
        - Preserve the student's original content - do NOT invent new material
        - Clean up formatting, add markdown headers/bullets where helpful
        - Return ONLY the JSON array
    """

    text_out, usage = generate_text(prompt_content, model=_MODEL, max_tokens=_MAX_TOKENS)
    return _parse_json(text_out), usage


def generate_test(
    section_content: str, section_title: str, test_format: str, test_duration_mins: int, section_index: int
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    if test_format == "mcq":
        q_type_instruction = """
            Generate multiple-choice questions. Each question must have:
            - "options": array of exactly 4 strings (A,B,C,D)
            - "correct_answer": the exact text of the correct option (not the letter)
        """
        json_example = """
        [
            {
                "question_no": 1,
                "question": "<question text>",
                "options": ["<option A>", "<option B>", "<option C>", "<option D>"],
                "correct_answer": "<correct answer text matching one of the options exactly>",
                "marks": 2
            }
        ]
        """
    elif test_format == "written":
        q_type_instruction = """
            Generate short answer/written questions. Each question must have:
            - Do NOT include the "options" key (it must be completely omitted or set to null/empty).
            - "correct_answer": a clear model answer text (since it is short answer, no options are shown).
        """
        json_example = """
        [
            {
                "question_no": 1,
                "question": "<question text>",
                "correct_answer": "<ideal model answer text>",
                "marks": 5
            }
        ]
        """
    else:
        q_type_instruction = """
            Generate a mix of MCQ questions and written questions.
            - For MCQ questions: include "options" (exactly 4 options) and "correct_answer" (matching one option).
            - For written questions: do NOT include "options", and "correct_answer" must be the model answer.
        """
        json_example = """
        [
            {
                "question_no": 1,
                "question": "<MCQ question text>",
                "options": ["<option A>", "<option B>", "<option C>", "<option D>"],
                "correct_answer": "<correct option text>",
                "marks": 2
            },
            {
                "question_no": 2,
                "question": "<Written question text>",
                "correct_answer": "<model answer text>",
                "marks": 5
            }
        ]
        """

    prompt_content = f"""
        You are a quiz generator. Create a test for the following study section.

        SECTION TITLE: {section_title}
        SECTION CONTENT: {section_content}

        TEST FORMAT: {test_format}
        TEST DURATION: {test_duration_mins} minutes

        {q_type_instruction}

        Return ONLY a valid JSON array of questions (no markdown fences) conforming to this format:
        {json_example}

        Rules:
        - Questions must be directly based on the section content
        - Do NOT invent facts not present in the content
        - Return ONLY the JSON array (no conversational text or extra explanation)
    """

    text, usage = generate_text(prompt_content, model=_MODEL, max_tokens=_MAX_TOKENS)
    questions = _parse_json(text)
    return {"format": test_format, "duration_mins": test_duration_mins, "questions": questions}, usage


def safe_serialise(obj: Any) -> Any:
    """Recursively convert ObjectId / datetime so json.dumps never crashes."""
    import datetime as dt

    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: safe_serialise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [safe_serialise(i) for i in obj]
    return obj


def evaluate(session_doc: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, int]]:
    sections = session_doc.get("sections", [])
    section_data = []
    for s in sections:
        answers = safe_serialise(s.get("submitted_answers", []))
        section_data.append({"title": s.get("title", "Untitled Section"), "answers": answers})

    prompt_content = f"""
        You are an expert academic evaluator.

        A student has completed a Pomodoro study session with {len(sections)} sections and tests.

        SESSION DATA: {json.dumps(section_data, indent=2)}

        Evaluate the student's performance and return ONLY a valid JSON object (no markdown fences):
        {{
            "overall_score": <number 0-100>,
            "total_marks_obtained": <number>,
            "total_marks_possible": <number>,
            "overall_feedback": "<2-3 sentence summary of performance>",
            "strengths": ["<strength 1>", "<strength 2>", "<strength 3>"],
            "weaknesses": ["<weakness 1>", "<weakness 2>"],
            "recommendations": ["<actionable recommendation 1>", "<recommendation 2>", "<recommendation 3>"],
            "section_results": [
                {{
                "section_index": 0,
                "section_title": "<title>",
                "score": <number 0-100>,
                "marks_obtained": <number>,
                "marks_possible": <number>,
                "ai_feedback": "<specific feedback for this section>"
                }}
            ]
        }}

        Be honest, specific, and constructive. Base evaluation solely on the submitted answers.
        Return ONLY the JSON object.
    """

    text, usage = generate_text(prompt_content, model=_MODEL, max_tokens=_MAX_TOKENS)
    return _parse_json(text), usage


def extract_uploaded_document_text(file_bytes: bytes, filename: str) -> Tuple[str, Dict[str, int]]:
    """AI-assisted upload: OCR/extract text from the uploaded file via Gemini."""
    mime = mimetypes.guess_type(filename)[0] or "application/pdf"
    prompt = "Extract ALL text from this document. Preserve structure and headings."
    return generate_content_from_file(file_bytes, mime, prompt)


def extract_written_answer(image_bytes: bytes, mime_type: str) -> str:
    """submit-test: OCR a photographed/handwritten answer via Gemini vision."""
    prompt = (
        "Extract the written answer from this student answer sheet. "
        "ONLY return the text of the answer. Do not add metadata or explanation."
    )
    text, _usage = generate_content_from_file(image_bytes, mime_type, prompt)
    return text.strip()
