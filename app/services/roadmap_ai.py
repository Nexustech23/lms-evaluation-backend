# ============================================================
# ROADMAP AI HELPERS
# Ported from controllers/self_learner/roadmap_controller.py.
#
# Uses its own lazy Anthropic/Gemini clients — mirroring the Flask
# original's own _get_anthropic()/_get_gemini() — rather than the shared
# app/services/claude.py / gemini.py helpers, because this flow needs
# access to stop_reason / finish_reason for truncation detection and
# Gemini's JSON response_mime_type config, which the simpler shared
# generate_text()/generate_content_from_file() helpers don't expose.
#
# All functions here are blocking SDK calls — run via asyncio.to_thread()
# from the router. Token-usage objects are always returned even when the
# response was truncated (Gemini/Claude bill for it regardless), so the
# router can increment usage before branching on the truncated flag —
# matching Flask's own ordering.
# ============================================================

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import anthropic
from bson import ObjectId
from google import genai as google_genai
from google.genai import types as google_genai_types
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings

logger = logging.getLogger(__name__)

_anthropic_client: Optional[anthropic.Anthropic] = None
_gemini_client: Optional[google_genai.Client] = None


def _get_anthropic() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _anthropic_client


def _get_gemini() -> google_genai.Client:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = google_genai.Client(api_key=settings.GEMINI_API_KEY)
    return _gemini_client


def extract_json(text: str) -> Any:
    """Robustly extract the first JSON object/array from a Claude/Gemini response.
    Handles markdown code-fences (```json … ```) and bare JSON."""
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fenced:
        text = fenced.group(1).strip()
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = text.find(start_char)
        if start == -1:
            continue
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == start_char:
                depth += 1
            elif ch == end_char:
                depth -= 1
                if depth == 0:
                    return json.loads(text[start:i + 1])
    raise ValueError("No valid JSON found in model output")


# ============================================================
# TOKEN TRACKING (per-user — distinct from the shared, institute-scoped
# helpers in app/utils/token_usage.py, which use a different document
# shape. Best-effort/non-fatal, matching Flask.)
# ============================================================

async def increment_student_claude_tokens(db: AsyncIOMotorDatabase, user_id: str, usage: Any) -> None:
    try:
        await db["users"].update_one(
            {"_id": ObjectId(user_id)},
            {"$inc": {
                "token_usage.claude.input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "token_usage.claude.output_tokens": getattr(usage, "output_tokens", 0) or 0,
            }},
        )
    except Exception as e:
        logger.warning("increment_student_claude_tokens failed (non-fatal): %s", e)


async def increment_student_gemini_tokens(db: AsyncIOMotorDatabase, user_id: str, usage_metadata: Any) -> None:
    try:
        await db["users"].update_one(
            {"_id": ObjectId(user_id)},
            {"$inc": {
                "token_usage.gemini.input_tokens": getattr(usage_metadata, "prompt_token_count", 0) or 0,
                "token_usage.gemini.output_tokens": getattr(usage_metadata, "candidates_token_count", 0) or 0,
            }},
        )
    except Exception as e:
        logger.warning("increment_student_gemini_tokens failed (non-fatal): %s", e)


# ============================================================
# CURRICULUM GENERATION (Claude — create_roadmap background job)
# ============================================================

def build_curriculum_prompt(
    subject, goal, skill_level, daily_study_time, revision_frequency, assessment_score,
    grounding_context: Optional[str] = None,
) -> str:
    score_context = (
        f"The student scored {assessment_score}% on the pre-assessment quiz, "
        f"so they already have some familiarity with the subject. "
        f"Adjust difficulty accordingly — avoid trivially basic content and start from where the assessment indicates."
        if assessment_score is not None
        else ""
    )
    # Optional RAG grounding: real content retrieved from a course material
    # the institute uploaded for this subject (see app/services/rag/). When
    # present, the curriculum should follow this document's actual structure
    # and weighting instead of the model's generic knowledge of the subject.
    grounding_block = (
        f"""
## Course Material (use this as the authoritative source for structure, topics, and emphasis)
The following was retrieved from a real course document uploaded for this subject. Ground the
roadmap's stages/topics/subtopics in what is actually here — do not invent topics that
contradict it, and prioritize what it emphasizes.

{grounding_context}
"""
        if grounding_context
        else ""
    )
    return f"""You are an expert curriculum designer and senior educator.
Your task is to create a **highly detailed, production-quality 4-Stage self-learning roadmap** for the following student profile.

## Student Profile
- Subject: {subject}
- Goal: {goal}
- Skill Level: {skill_level}
- Daily Study Time: {daily_study_time}
- Revision Frequency: {revision_frequency}
{score_context}
{grounding_block}

## Roadmap Requirements
Generate exactly **4 progressive learning Stages** that form a complete, structured curriculum.

### Stage naming rules (name each stage accurately based on the subject):
- Stage 1: Foundations & Core Concepts
- Stage 2: Intermediate Application
- Stage 3: Advanced Techniques & Problem Solving
- Stage 4: Expert Mastery & Real-World Projects

### Topic & Subtopic Requirements
Each stage must have **3 to 5 topics**. Each topic must have **4 to 7 subtopics**.
Every subtopic title must be specific, actionable, and unique. Never use generic names like "Introduction" alone.

### Curriculum Stats (generate realistic estimates)
- estimatedWeeks: realistic number of weeks to complete this stage at {daily_study_time}/day
- totalTopics: total subtopic count for the stage
- difficultyScore: integer 1–10

### Practice Questions (per Stage)
Generate **10 MCQ practice questions** per stage that target conceptual understanding.
Each question: {{ "question": "...", "options": ["A", "B", "C", "D"], "answer": <0-indexed int>, "explanation": "..." }}

## Output Format
Return ONLY a valid JSON object matching this exact schema. No prose, no markdown, only JSON.

{{
  "subject_display_name": "Full display name of the subject",
  "stats": {{
    "estimatedWeeks": <total across all stages>,
    "totalTopics": <total subtopic count>,
    "difficultyScore": <1-10>
  }},
  "levels": [
    {{
      "level": 1,
      "title": "<Stage 1 name — tailored to {subject}>",
      "description": "<One sentence summary of what the student masters in this stage>",
      "estimatedWeeks": <int>,
      "topics": [
        {{
          "title": "<Topic title>",
          "description": "<Why this topic matters>",
          "subtopics": [
            {{
              "title": "<Specific subtopic — must be unique and actionable>",
              "summary": "<2-3 sentence overview of what the student will learn in this subtopic>",
              "keyPoints": ["<point 1>", "<point 2>", "<point 3>"],
              "difficulty": "Beginner | Intermediate | Advanced"
            }}
          ]
        }}
      ],
      "practiceQuestions": [
        {{
          "question": "<Question text>",
          "options": ["<A>", "<B>", "<C>", "<D>"],
          "answer": <0-indexed correct option int>,
          "explanation": "<Brief explanation of why the answer is correct>"
        }}
      ]
    }},
    {{
      "level": 2,
      "title": "<Stage 2 name>",
      "description": "...",
      "estimatedWeeks": <int>,
      "topics": [ ... ],
      "practiceQuestions": [ ... ]
    }},
    {{
      "level": 3,
      "title": "<Stage 3 name>",
      "description": "...",
      "estimatedWeeks": <int>,
      "topics": [ ... ],
      "practiceQuestions": [ ... ]
    }},
    {{
      "level": 4,
      "title": "<Stage 4 name>",
      "description": "...",
      "estimatedWeeks": <int>,
      "topics": [ ... ],
      "practiceQuestions": [ ... ]
    }}
  ]
}}"""


def generate_curriculum(prompt: str) -> Tuple[Optional[Dict[str, Any]], Any, bool]:
    """Returns (curriculum_dict_or_None, usage, truncated). May raise anthropic.APIError."""
    client = _get_anthropic()
    start_time = time.time()
    message = client.messages.create(
        model="claude-sonnet-4-5", max_tokens=25000, messages=[{"role": "user", "content": prompt}]
    )
    logger.info("Claude curriculum call took %.2fs", time.time() - start_time)

    if message.stop_reason == "max_tokens":
        return None, message.usage, True

    return extract_json(message.content[0].text), message.usage, False


# ============================================================
# GEMINI JSON GENERATION (subtopic notes / stage quiz / pre-assessment)
# ============================================================

def generate_gemini_json(prompt: str) -> Tuple[Optional[Any], Any, bool]:
    """Returns (parsed_json_or_None, usage_metadata, truncated)."""
    client = _get_gemini()
    start_time = time.time()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=google_genai_types.GenerateContentConfig(response_mime_type="application/json"),
    )
    logger.info("Gemini call took %.2fs", time.time() - start_time)

    usage = response.usage_metadata
    truncated = response.candidates[0].finish_reason.name == "MAX_TOKENS"
    if truncated:
        return None, usage, True

    return extract_json(response.text), usage, False


# ============================================================
# PROMPT BUILDERS (notes / stage quiz / pre-assessment)
# ============================================================

def build_notes_prompt(
    subject: str, stage_title: str, topic_title: str, sub_title: str, sub_summary: str, key_points: List[str],
    grounding_context: Optional[str] = None,
) -> str:
    grounding_block = (
        f"""
## Course Material (ground the notes in this real content where relevant)
{grounding_context}
"""
        if grounding_context
        else ""
    )
    return f"""You are a senior technical educator writing premium self-study notes.

## Context
Subject: {subject}
Stage: {stage_title}
Topic: {topic_title}
Subtopic: {sub_title}
Overview: {sub_summary}
Key Points to Cover: {json.dumps(key_points)}
{grounding_block}

## Task
Write **comprehensive, student-friendly study notes** for the subtopic "{sub_title}".
The notes must help a student both understand and apply the concept.

Return ONLY a valid JSON object (no prose, no markdown wrapper) with this exact schema:

{{
  "summary": "<3-5 sentence engaging overview of what this subtopic covers and why it matters>",
  "detailedExplanation": [
    {{ "heading": "<Section title, e.g. What is it?>", "content": "<2-4 paragraphs for this section>" }},
    {{ "heading": "<Section title, e.g. How it works>", "content": "<2-4 paragraphs>" }},
    {{ "heading": "<Section title, e.g. Real-world example>", "content": "<2-4 paragraphs>" }},
    {{ "heading": "<Section title, e.g. When to use it>", "content": "<2-4 paragraphs>" }}
  ],
  "keyPoints": [
    "<Concise, memorable bullet — start with a verb>",
    "<point 2>",
    "<point 3>",
    "<point 4>",
    "<point 5>"
  ],
  "formulasOrRules": [
    {{
      "name": "<Formula/Rule name>",
      "formula": "<The actual formula, rule, or pattern>",
      "explanation": "<When and how to use it>"
    }}
  ],
  "codeExample": {{
    "language": "<programming language or 'N/A'>",
    "code": "<relevant code snippet or 'N/A' if not applicable>",
    "explanation": "<Line-by-line or block-by-block walkthrough>"
  }},
  "commonMistakes": [
    "<Mistake students commonly make and how to avoid it>",
    "<mistake 2>",
    "<mistake 3>"
  ],
  "interviewTips": [
    "<High-frequency interview question or tip related to this subtopic>",
    "<tip 2>",
    "<tip 3>"
  ],
  "revisionChecklist": [
    "<I can explain ... >",
    "<I can implement ... >",
    "<I can identify ... >"
  ]
}}"""


def build_stage_quiz_prompt(subject: str, stage_title: str, topic_names: List[str], subtopic_names: List[str]) -> str:
    return f"""You are an expert technical examiner designing a rigorous stage-completion quiz.

## Context
Subject: {subject}
Stage: {stage_title}
Topics Covered: {json.dumps(topic_names)}
All Subtopics: {json.dumps(subtopic_names)}

## Task
Create exactly **10 MCQ questions** that assess the student's understanding of the above stage.

Rules:
- Questions must vary in difficulty: 3 easy, 4 medium, 3 hard
- Cover concepts from all topics evenly
- Options must be plausible (no obviously wrong distractors)
- The correct answer index is 0-based (0, 1, 2, or 3)
- Include a clear explanation for the correct answer

Return ONLY a valid JSON array (no prose, no markdown):

[
  {{
    "question": "<Question text>",
    "options": ["<A>", "<B>", "<C>", "<D>"],
    "answer": <0-indexed correct int>,
    "explanation": "<Why this is the correct answer>",
    "difficulty": "Easy | Medium | Hard",
    "topic": "<Which topic this question tests>"
  }}
]"""


def build_pre_assessment_prompt(subject: str) -> str:
    return f"""You are an expert educator designing a beginner-level knowledge assessment quiz.

## Context
Subject: {subject}
Level: Beginner (no prior knowledge assumed)
Purpose: Pre-assessment to gauge the student's starting knowledge before creating their learning roadmap.

## Task
Create exactly **10 MCQ questions** that assess fundamental beginner-level understanding of {subject}.

Rules:
- All questions must be at beginner level — suitable for someone just starting to learn {subject}
- Cover a broad range of fundamental concepts (not one narrow topic)
- Each question must have exactly 4 options (A, B, C, D style)
- Options must be plausible — avoid obviously wrong distractors
- The correct answer index is 0-based (0 = first option, 1 = second, etc.)
- No trick questions; test understanding, not memorization of obscure facts

Return ONLY a valid JSON array (no prose, no markdown fences):

[
  {{
    "question": "<Clear, beginner-level question text>",
    "options": ["<Option A>", "<Option B>", "<Option C>", "<Option D>"],
    "answer": <0-indexed correct option integer>
  }}
]"""
