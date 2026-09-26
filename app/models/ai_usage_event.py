# ============================================================
# aiUsageEvents — one document per AI provider call across the MyCareerGuru
# (self-learner) product surface: roadmap generation, self-review
# (homework help / notes), Test Engine, detailed feedback, and the RAG
# pipeline that grounds roadmap generation in uploaded course material.
#
# Plain-dict convention (no ODM), matching every other app/models/*.py file.
# Written exclusively through app/services/ai_usage.py::record_ai_usage() —
# never insert into this collection directly, so cost estimation and the
# users.token_usage rollup stay in sync.
# ============================================================
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bson import ObjectId


class Provider:
    CLAUDE = "claude"
    GEMINI = "gemini"


class Feature:
    """Closed vocabulary for the `feature` tag — keep this list in sync with
    every record_ai_usage() call site so per-feature dashboards stay legible
    instead of accumulating ad-hoc free-text tags."""

    # Roadmap (app/api/routers/roadmap.py)
    ROADMAP_CURRICULUM = "roadmap_curriculum"
    ROADMAP_PRE_ASSESSMENT = "roadmap_pre_assessment"
    ROADMAP_NOTES = "roadmap_notes"
    ROADMAP_RESOURCES = "roadmap_resources"
    ROADMAP_QUIZ_GENERATE = "roadmap_quiz_generate"
    ROADMAP_QUIZ_GRADING = "roadmap_quiz_grading"
    ROADMAP_PRACTICE_QUESTIONS = "roadmap_practice_questions"
    ROADMAP_PRACTICE_EVALUATE = "roadmap_practice_evaluate"
    ROADMAP_DIAGRAM_REPAIR = "roadmap_diagram_repair"

    # Self-review (app/api/routers/ai_tutor.py — homework help & AI notes)
    SELF_REVIEW_HOMEWORK_HELP = "self_review_homework_help"
    SELF_REVIEW_HOMEWORK_EXTRACTION = "self_review_homework_extraction"
    SELF_REVIEW_NOTES = "self_review_notes"
    SELF_REVIEW_NOTES_EXTRACTION = "self_review_notes_extraction"

    # Test Engine (app/services/mock_test_generation.py, app/api/routers/mock_tests.py)
    TEST_ENGINE_GENERATE = "test_engine_generate"
    TEST_ENGINE_GRADING = "test_engine_grading"

    # Detailed feedback (app/services/attempt_insight.py)
    DETAILED_FEEDBACK = "detailed_feedback"

    # RAG pipeline (app/services/rag/*)
    RAG_INGEST_EXTRACTION = "rag_ingest_extraction"
    RAG_EMBEDDING = "rag_embedding"
    RAG_SUMMARIZE = "rag_summarize"
    RAG_RETRIEVE = "rag_retrieve"

    # Faculty — question paper generation (app/api/routers/question_paper.py)
    QP_QUESTION_BANK_EXTRACTION = "qp_question_bank_extraction"
    QP_COURSE_PLANNER_EXTRACTION = "qp_course_planner_extraction"
    QP_GENERATION = "qp_generation"

    # Faculty — answer-script grading (app/api/routers/grading.py)
    GRADING_ANSWER_OCR = "grading_answer_ocr"
    GRADING_ANSWER_EVALUATION = "grading_answer_evaluation"
    GRADING_TRANSCRIPT_GENERATION = "grading_transcript_generation"

    # Faculty — exam question-paper text extraction (app/services/gemini.py)
    EXAM_QUESTION_PAPER_EXTRACTION = "exam_question_paper_extraction"

    # Faculty — one-time MCQ answer-key determination (app/services/mcq_grading.py),
    # runs once per exam at question-paper-upload time, not once per student.
    GRADING_MCQ_ANSWER_KEY = "grading_mcq_answer_key"


# Human-readable label per Feature code, for activity-log dashboards
# (app/api/routers/profile.py's /institute-students/activity-logs and
# /self-learners/activity-logs). Kept next to Feature so the two stay in
# sync — every Feature constant should have an entry here.
FEATURE_LABELS: Dict[str, str] = {
    Feature.ROADMAP_CURRICULUM: "Generated roadmap",
    Feature.ROADMAP_PRE_ASSESSMENT: "Took pre-assessment quiz",
    Feature.ROADMAP_NOTES: "Generated topic notes",
    Feature.ROADMAP_RESOURCES: "Fetched learning resources",
    Feature.ROADMAP_QUIZ_GENERATE: "Generated week quiz",
    Feature.ROADMAP_QUIZ_GRADING: "Quiz auto-graded",
    Feature.ROADMAP_PRACTICE_QUESTIONS: "Generated practice questions",
    Feature.ROADMAP_PRACTICE_EVALUATE: "Practice answer evaluated",
    Feature.ROADMAP_DIAGRAM_REPAIR: "Regenerated roadmap diagram",
    Feature.SELF_REVIEW_HOMEWORK_HELP: "Asked AI Tutor for homework help",
    Feature.SELF_REVIEW_HOMEWORK_EXTRACTION: "Uploaded homework for AI Tutor",
    Feature.SELF_REVIEW_NOTES: "Generated AI Tutor notes",
    Feature.SELF_REVIEW_NOTES_EXTRACTION: "Uploaded material for AI Tutor notes",
    Feature.TEST_ENGINE_GENERATE: "Generated practice test",
    Feature.TEST_ENGINE_GRADING: "Practice test graded",
    Feature.DETAILED_FEEDBACK: "Viewed detailed feedback",
    Feature.RAG_INGEST_EXTRACTION: "Uploaded course material",
    Feature.RAG_EMBEDDING: "Course material indexed",
    Feature.RAG_SUMMARIZE: "Course material summarized",
    Feature.RAG_RETRIEVE: "Course material referenced",
    Feature.QP_QUESTION_BANK_EXTRACTION: "Extracted question bank",
    Feature.QP_COURSE_PLANNER_EXTRACTION: "Extracted course planner",
    Feature.QP_GENERATION: "Generated question paper",
    Feature.GRADING_ANSWER_OCR: "Extracted answer script text",
    Feature.GRADING_ANSWER_EVALUATION: "Graded answer script",
    Feature.GRADING_TRANSCRIPT_GENERATION: "Generated answer transcript",
    Feature.EXAM_QUESTION_PAPER_EXTRACTION: "Extracted question paper text",
    Feature.GRADING_MCQ_ANSWER_KEY: "Determined MCQ answer key",
}


def feature_label(feature: str) -> str:
    return FEATURE_LABELS.get(feature, feature)


# Which surface (student-facing MyCareerGuru vs faculty-facing exam tooling)
# a feature belongs to — used by GET /ai-usage's `scope` filter and the
# admin/super-admin Activity Logs pages' Students/Faculty tab, so both can
# split the shared aiUsageEvents ledger without duplicating a feature list
# in the frontend.
FACULTY_FEATURES = frozenset({
    Feature.QP_QUESTION_BANK_EXTRACTION,
    Feature.QP_COURSE_PLANNER_EXTRACTION,
    Feature.QP_GENERATION,
    Feature.GRADING_ANSWER_OCR,
    Feature.GRADING_ANSWER_EVALUATION,
    Feature.GRADING_TRANSCRIPT_GENERATION,
    Feature.EXAM_QUESTION_PAPER_EXTRACTION,
    Feature.GRADING_MCQ_ANSWER_KEY,
})

STUDENT_FEATURES = frozenset(FEATURE_LABELS) - FACULTY_FEATURES


def create_ai_usage_event_document(
    *,
    user_id: str,
    provider: str,
    model: str,
    feature: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    grounded: Optional[bool] = None,
    context_id: Optional[str] = None,
    job_id: Optional[str] = None,
    institute_id: Optional[str] = None,
    school_id: Optional[str] = None,
    programme_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "user_id": ObjectId(user_id),
        "tenant_type": "institute" if institute_id else "individual",
        "institute_id": ObjectId(institute_id) if institute_id else None,
        "school_id": ObjectId(school_id) if school_id else None,
        "programme_id": ObjectId(programme_id) if programme_id else None,
        "provider": provider,
        "model": model,
        "feature": feature,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_usd": cost_usd,
        "grounded": grounded,
        "context_id": context_id,
        "job_id": job_id,
        "created_at": datetime.now(timezone.utc),
    }


def serialize_ai_usage_event(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(doc)
    out["_id"] = str(out["_id"])
    out["user_id"] = str(out["user_id"])
    for field in ("institute_id", "school_id", "programme_id"):
        if out.get(field):
            out[field] = str(out[field])
    if out.get("created_at"):
        out["created_at"] = out["created_at"].isoformat()
    return out
