# ============================================================
# ROADMAP ROUTER
# Ported from routes/self_learner/roadmap_routes.py +
# controllers/self_learner/roadmap_controller.py.
#
# Flask's blueprint is mounted at url_prefix="/api/self-learner/roadmap" —
# mirrored here with prefix="/api/self-learner/roadmap" (this is also the
# one blueprint the Next.js frontend's rewrite config keeps the /api
# prefix for — see next.config.mjs).
#
# Async curriculum generation uses the existing Redis-backed job_store.py +
# BackgroundTasks pattern (matching ai_tutor.py/pomodoro.py) instead of
# Flask's raw in-memory dict (_creation_jobs — explicitly noted in Flask as
# only safe for a "single-process Flask app"), per the migration plan.
# Job status values ("processing"/"done"/"error") are kept as Flask's own
# strings since the self-learner frontend's poller already expects them.
#
# Job-status endpoint (GET /status/{job_id}) is scoped to the requesting
# user — Flask's original was not (any authenticated caller who knew/guessed
# a job_id could poll it); fixed here by embedding user_id in the job
# payload at creation and checking it on lookup (same fix applied to
# pomodoro's job endpoint).
# ============================================================

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import anthropic
from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from app.api.deps import get_current_identity
from app.db.mongodb import get_database
from app.models.roadmap import create_roadmap_document, serialize_roadmap
from app.schemas.roadmap import (
    CreateRoadmapRequest,
    PreAssessmentRequest,
    SubmitQuizRequest,
    UpdateSubtopicRequest,
)
from app.services.job_store import get_job, set_job, update_job
from app.services.rag import mongo_store as rag_mongo_store
from app.services.rag import singletons as rag_singletons
from app.services.rag.retrieval import router as rag_router
from app.services.roadmap_ai import (
    build_curriculum_prompt,
    build_notes_prompt,
    build_pre_assessment_prompt,
    build_stage_quiz_prompt,
    generate_curriculum,
    generate_gemini_json,
    increment_student_claude_tokens,
    increment_student_gemini_tokens,
)

router = APIRouter(prefix="/api/self-learner/roadmap", dependencies=[Depends(get_current_identity)], tags=["roadmap"])

ROADMAP_JOB_PREFIX = "roadmap_job:"


# ============================================================
# PRIVATE HELPERS (ported verbatim from the Flask controller)
# ============================================================

def _is_level_unlocked(doc: Dict[str, Any], level: int) -> bool:
    return level in doc.get("unlockedLevels", [1])


def _find_level(doc: Dict[str, Any], level: int) -> Optional[Dict[str, Any]]:
    return next((item for item in doc.get("levels", []) if item.get("level") == level), None)


def _is_subtopic_key_valid(doc: Dict[str, Any], subtopic_key: str) -> bool:
    for lvl in doc.get("levels", []):
        level = lvl.get("level")
        for topic_idx, topic in enumerate(lvl.get("topics", [])):
            for subtopic in topic.get("subtopics", []):
                if subtopic_key == f"{level}-{topic_idx}-{subtopic.get('title')}":
                    return True
    return False


def _sanitize_quiz_for_history(quiz: List[Dict[str, Any]], results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_idx = {item.get("questionIdx"): item for item in results}
    safe_questions = []
    for idx, question in enumerate(quiz):
        result = by_idx.get(idx, {})
        safe_questions.append({
            "questionIdx": idx,
            "question": question.get("question", ""),
            "options": question.get("options", []),
            "yourAnswer": result.get("yourAnswer"),
            "correctAnswer": result.get("correctAnswer"),
            "isCorrect": result.get("isCorrect", False),
            "explanation": result.get("explanation", ""),
            "difficulty": question.get("difficulty", ""),
            "topic": question.get("topic", ""),
        })
    return safe_questions


async def _get_grounding_context(
    db: AsyncIOMotorDatabase, subject: str, query: str, user_id: str
) -> Optional[str]:
    """
    Best-effort RAG grounding lookup: if course material has been uploaded
    (via app/api/routers/course_material.py) for a subject matching this
    roadmap's subject name, retrieve relevant content to ground the AI
    generation in. Returns None (not raises) on any failure or when no
    matching/usable material exists — generation must always be able to
    fall back to ungrounded, exactly as it worked before this RAG port.
    """
    try:
        record = await rag_mongo_store.find_document_for_subject(db, subject)
        if record is None:
            return None

        vector_store = None
        if record.doc_type.value == "unstructured":
            vector_store = await asyncio.to_thread(rag_singletons.get_vector_store)
            if vector_store is None:
                return None

        result = await rag_router.retrieve(
            query, record.id, record.doc_type, db, user_id=user_id, vector_store=vector_store,
        )
        if not rag_router.should_use_rag(result):
            return None
        return result.context_text
    except Exception as e:
        logging.warning("roadmap: RAG grounding lookup failed (falling back to ungrounded): %s", e)
        return None


def _recalculate_progress(doc: Dict[str, Any]) -> int:
    levels = doc.get("levels", [])
    progress = doc.get("progress", {})
    completed_sub = progress.get("completedSubtopics", [])
    passed_quizzes = progress.get("passedQuizzes", {})

    total_sub = sum(len(topic.get("subtopics", [])) for lvl in levels for topic in lvl.get("topics", []))

    total_actions = total_sub + 4  # subtopics + 4 passed stage quizzes
    completed_actions = len(completed_sub) + len(passed_quizzes)
    return min(100, round((completed_actions / total_actions * 100))) if total_actions > 0 else 0


# ============================================================
# BACKGROUND JOB — ROADMAP CREATION
# ============================================================

async def _run_create_roadmap_job(
    job_id: str, user_id: str, subject: str, goal: str, skill_level: str,
    daily_study_time: str, revision_frequency: str, assessment_score: Optional[float],
) -> None:
    db = get_database()
    try:
        await update_job(ROADMAP_JOB_PREFIX, job_id, {"step": "Checking for course material to ground the roadmap in…"})
        grounding_context = await _get_grounding_context(
            db, subject, f"Curriculum structure, topics, and assessment weighting for: {subject} — {goal}", user_id,
        )

        await update_job(ROADMAP_JOB_PREFIX, job_id, {"step": "Generating curriculum with AI…"})

        prompt = build_curriculum_prompt(
            subject, goal, skill_level, daily_study_time, revision_frequency, assessment_score,
            grounding_context=grounding_context,
        )

        try:
            curriculum, usage, truncated = await asyncio.to_thread(generate_curriculum, prompt)
        except anthropic.APIError as e:
            logging.error("Anthropic API error in roadmap job %s: %s", job_id, e)
            await update_job(ROADMAP_JOB_PREFIX, job_id, {"status": "error", "error": f"AI generation failed: {e}"})
            return

        await increment_student_claude_tokens(db, user_id, usage)

        if truncated:
            logging.error("Claude curriculum response truncated — max_tokens limit hit (job %s)", job_id)
            await update_job(ROADMAP_JOB_PREFIX, job_id, {
                "status": "error", "error": "AI response was too long. Try a more specific subject or goal.",
            })
            return

        unlocked = [1]
        if assessment_score is not None and assessment_score >= 80:
            unlocked.append(2)

        user_object_id = ObjectId(user_id)

        await db["selfLearnerRoadmaps"].update_many({"user_id": user_object_id}, {"$set": {"active": False}})

        doc_data = {
            "subject": curriculum.get("subject_display_name", subject),
            "goal": goal,
            "skill_level": skill_level,
            "daily_study_time": daily_study_time,
            "revision_frequency": revision_frequency,
            "assessment_score": assessment_score,
            "stats": curriculum.get("stats", {}),
            "levels": curriculum.get("levels", []),
            "unlockedLevels": unlocked,
        }
        doc = create_roadmap_document(user_id, doc_data)
        result = await db["selfLearnerRoadmaps"].insert_one(doc)

        logging.info("Roadmap created for user %s, subject=%s", user_id, subject)
        await update_job(ROADMAP_JOB_PREFIX, job_id, {
            "status": "done", "roadmap_id": str(result.inserted_id), "step": "Done",
        })

    except Exception as e:
        logging.error("roadmap creation job %s failed: %s", job_id, e, exc_info=True)
        await update_job(ROADMAP_JOB_PREFIX, job_id, {
            "status": "error", "error": "Internal server error during roadmap generation.",
        })


# ── Roadmap creation job status (must be before /{roadmap_id} routes) ──────

@router.get("/status/{job_id}")
async def get_creation_status(job_id: str, identity: dict = Depends(get_current_identity)):
    job = await get_job(ROADMAP_JOB_PREFIX, job_id)
    if job is None or job.get("user_id") != identity["user_id"]:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ── Pre-Assessment Quiz (must be before /{roadmap_id} routes) ──────────────

@router.post("/assess")
async def generate_pre_assessment(
    payload: PreAssessmentRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    subject = payload.subject
    prompt = build_pre_assessment_prompt(subject)

    try:
        questions, usage, truncated = await asyncio.to_thread(generate_gemini_json, prompt)
    except Exception as e:
        logging.error("generate_pre_assessment_quiz_controller: %s", e, exc_info=True)
        raise HTTPException(status_code=502, detail=f"AI quiz generation failed: {e}")

    await increment_student_gemini_tokens(db, identity["user_id"], usage)

    if truncated:
        logging.error("Gemini pre-assessment response truncated — MAX_TOKENS limit hit")
        raise HTTPException(status_code=502, detail="AI response was too long. Please try again.")

    if not isinstance(questions, list):
        raise HTTPException(status_code=502, detail="AI returned unexpected format. Please try again.")

    logging.info("Pre-assessment quiz generated: subject=%s questions=%d", subject, len(questions))
    return {"questions": questions}


# ── List & Create ───────────────────────────────────────────────────────────

@router.post("")
async def create_roadmap(
    background_tasks: BackgroundTasks,
    payload: CreateRoadmapRequest,
    identity: dict = Depends(get_current_identity),
):
    subject = payload.subject
    goal = payload.goal
    skill_level = (payload.skill_level or "Beginner").strip()
    daily_study_time = (payload.daily_study_time or "1 Hour").strip()
    revision_frequency = (payload.revision_frequency or "Every Week").strip()
    assessment_score = payload.assessment_score

    job_id = str(uuid.uuid4())
    await set_job(ROADMAP_JOB_PREFIX, job_id, {
        "status": "processing", "step": "Starting…", "user_id": identity["user_id"],
    })

    background_tasks.add_task(
        _run_create_roadmap_job, job_id, identity["user_id"], subject, goal,
        skill_level, daily_study_time, revision_frequency, assessment_score,
    )

    return JSONResponse(status_code=202, content={"job_id": job_id, "status": "processing"})


@router.get("")
async def get_roadmaps(
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    docs = [
        d async for d in
        db["selfLearnerRoadmaps"].find({"user_id": ObjectId(identity["user_id"])}).sort("created_at", -1)
    ]
    return [serialize_roadmap(d) for d in docs]


# ── Single Roadmap ──────────────────────────────────────────────────────────

@router.get("/{roadmap_id}")
async def get_roadmap(
    roadmap_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(roadmap_id):
        raise HTTPException(status_code=400, detail="Invalid roadmap id")

    doc = await db["selfLearnerRoadmaps"].find_one(
        {"_id": ObjectId(roadmap_id), "user_id": ObjectId(identity["user_id"])}
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Roadmap not found")

    return serialize_roadmap(doc)


# ── Subtopic Progress ───────────────────────────────────────────────────────

@router.patch("/{roadmap_id}/subtopic")
async def update_subtopic(
    roadmap_id: str,
    payload: UpdateSubtopicRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(roadmap_id):
        raise HTTPException(status_code=400, detail="Invalid roadmap id")

    subtopic_key = payload.subtopic_key
    completed = payload.completed

    user_object_id = ObjectId(identity["user_id"])
    roadmap_object_id = ObjectId(roadmap_id)

    doc = await db["selfLearnerRoadmaps"].find_one({"_id": roadmap_object_id, "user_id": user_object_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Roadmap not found")
    if not _is_subtopic_key_valid(doc, subtopic_key):
        raise HTTPException(status_code=400, detail="Invalid subtopic key")

    try:
        level = int(str(subtopic_key).split("-", 1)[0])
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid subtopic key")
    if not _is_level_unlocked(doc, level):
        raise HTTPException(status_code=403, detail=f"Stage {level} is locked. Pass previous stage quizzes to unlock it.")

    sub_list = doc["progress"]["completedSubtopics"]
    if completed:
        if subtopic_key not in sub_list:
            sub_list.append(subtopic_key)
    else:
        if subtopic_key in sub_list:
            sub_list.remove(subtopic_key)

    new_progress = _recalculate_progress(doc)
    doc["progress"]["overallProgress"] = new_progress

    updated_doc = await db["selfLearnerRoadmaps"].find_one_and_update(
        {"_id": roadmap_object_id, "user_id": user_object_id},
        {"$set": {
            "progress.completedSubtopics": sub_list,
            "progress.overallProgress": new_progress,
            "updated_at": datetime.now(timezone.utc),
        }},
        return_document=ReturnDocument.AFTER,
    )

    return serialize_roadmap(updated_doc)


# ── AI Study Notes (on-demand, cached) ──────────────────────────────────────

@router.get("/{roadmap_id}/notes")
async def get_subtopic_notes(
    roadmap_id: str,
    level: int = Query(1),
    topic_idx: int = Query(0),
    subtopic_idx: int = Query(0),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(roadmap_id):
        raise HTTPException(status_code=400, detail="Invalid roadmap id")

    user_object_id = ObjectId(identity["user_id"])
    roadmap_object_id = ObjectId(roadmap_id)

    doc = await db["selfLearnerRoadmaps"].find_one({"_id": roadmap_object_id, "user_id": user_object_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Roadmap not found")
    if not _is_level_unlocked(doc, level):
        raise HTTPException(status_code=403, detail=f"Stage {level} is locked. Pass previous stage quizzes to unlock it.")

    level_data = _find_level(doc, level)
    if not level_data:
        raise HTTPException(status_code=404, detail=f"Level {level} not found in roadmap")

    topics = level_data.get("topics", [])
    if topic_idx >= len(topics):
        raise HTTPException(status_code=400, detail="topic_idx out of range")

    subtopics = topics[topic_idx].get("subtopics", [])
    if subtopic_idx >= len(subtopics):
        raise HTTPException(status_code=400, detail="subtopic_idx out of range")

    subtopic = subtopics[subtopic_idx]

    if subtopic.get("notes"):
        return {"notes": subtopic["notes"], "cached": True}

    subject = doc.get("subject", "")
    topic_title = topics[topic_idx].get("title", "")
    sub_title = subtopic.get("title", "")
    sub_summary = subtopic.get("summary", "")
    key_points = subtopic.get("keyPoints", [])

    grounding_context = await _get_grounding_context(
        db, subject, f"{topic_title} — {sub_title}: {sub_summary}", identity["user_id"],
    )
    prompt = build_notes_prompt(
        subject, level_data.get("title", ""), topic_title, sub_title, sub_summary, key_points,
        grounding_context=grounding_context,
    )

    try:
        notes, usage, truncated = await asyncio.to_thread(generate_gemini_json, prompt)
    except Exception as e:
        logging.error("generate_subtopic_notes_controller: %s", e, exc_info=True)
        raise HTTPException(status_code=502, detail=f"AI notes generation failed: {e}")

    await increment_student_gemini_tokens(db, identity["user_id"], usage)

    if truncated:
        logging.error("Gemini notes response truncated — MAX_TOKENS limit hit")
        raise HTTPException(status_code=502, detail="AI response was too long. Try a more specific subtopic.")

    await db["selfLearnerRoadmaps"].update_one(
        {"_id": roadmap_object_id, "user_id": user_object_id},
        {"$set": {
            f"levels.$[lvl].topics.$[top].subtopics.{subtopic_idx}.notes": notes,
            "updated_at": datetime.now(timezone.utc),
        }},
        array_filters=[{"lvl.level": level}, {"top.title": topics[topic_idx].get("title")}],
    )

    logging.info("Notes generated: roadmap=%s L%s T%s S%s", roadmap_id, level, topic_idx, subtopic_idx)
    return {"notes": notes, "cached": False}


# ── AI Stage Quiz (on-demand, cached) ───────────────────────────────────────

@router.get("/{roadmap_id}/quiz")
async def get_stage_quiz(
    roadmap_id: str,
    level: int = Query(1),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(roadmap_id):
        raise HTTPException(status_code=400, detail="Invalid roadmap id")

    user_object_id = ObjectId(identity["user_id"])
    roadmap_object_id = ObjectId(roadmap_id)

    doc = await db["selfLearnerRoadmaps"].find_one({"_id": roadmap_object_id, "user_id": user_object_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Roadmap not found")
    if not _is_level_unlocked(doc, level):
        raise HTTPException(status_code=403, detail=f"Stage {level} is locked. Pass previous stage quizzes to unlock it.")

    level_data = _find_level(doc, level)
    if not level_data:
        raise HTTPException(status_code=404, detail=f"Level {level} not found")

    cached_quiz = level_data.get("stageQuiz")
    if cached_quiz:
        return {"quiz": cached_quiz, "cached": True}

    subject = doc.get("subject", "")
    stage_title = level_data.get("title", f"Stage {level}")
    topic_names: List[str] = []
    subtopic_names: List[str] = []
    for t in level_data.get("topics", []):
        topic_names.append(t.get("title", ""))
        for s in t.get("subtopics", []):
            subtopic_names.append(s.get("title", ""))

    prompt = build_stage_quiz_prompt(subject, stage_title, topic_names, subtopic_names)

    try:
        quiz, usage, truncated = await asyncio.to_thread(generate_gemini_json, prompt)
    except Exception as e:
        logging.error("generate_stage_quiz_controller: %s", e, exc_info=True)
        raise HTTPException(status_code=502, detail=f"AI quiz generation failed: {e}")

    await increment_student_gemini_tokens(db, identity["user_id"], usage)

    if truncated:
        logging.error("Gemini quiz response truncated — MAX_TOKENS limit hit")
        raise HTTPException(status_code=502, detail="AI response was too long. Please try again.")

    await db["selfLearnerRoadmaps"].update_one(
        {"_id": roadmap_object_id, "user_id": user_object_id},
        {"$set": {"levels.$[lvl].stageQuiz": quiz, "updated_at": datetime.now(timezone.utc)}},
        array_filters=[{"lvl.level": level}],
    )

    logging.info("Stage quiz generated: roadmap=%s level=%s", roadmap_id, level)
    return {"quiz": quiz, "cached": False}


# ── AI Quiz Submission & Grading ────────────────────────────────────────────

@router.post("/{roadmap_id}/quiz/submit")
async def submit_quiz(
    roadmap_id: str,
    payload: SubmitQuizRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(roadmap_id):
        raise HTTPException(status_code=400, detail="Invalid roadmap id")

    user_object_id = ObjectId(identity["user_id"])
    roadmap_object_id = ObjectId(roadmap_id)

    level = payload.level
    answers = payload.answers

    doc = await db["selfLearnerRoadmaps"].find_one({"_id": roadmap_object_id, "user_id": user_object_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Roadmap not found")
    if not _is_level_unlocked(doc, level):
        raise HTTPException(status_code=403, detail=f"Stage {level} is locked. Pass previous stage quizzes to unlock it.")

    level_data = _find_level(doc, level)
    if not level_data:
        raise HTTPException(status_code=404, detail=f"Level {level} not found")

    quiz = level_data.get("stageQuiz")
    if not quiz:
        raise HTTPException(status_code=400, detail="Stage quiz not generated yet. Call GET /quiz first.")

    correct_count = 0
    wrong_topics: List[str] = []
    results_detail = []

    for idx, q in enumerate(quiz):
        student_answer = answers.get(str(idx))
        correct_answer = q.get("answer")
        is_correct = student_answer == correct_answer

        if is_correct:
            correct_count += 1
        else:
            topic_label = q.get("topic", "")
            if topic_label and topic_label not in wrong_topics:
                wrong_topics.append(topic_label)

        results_detail.append({
            "questionIdx": idx,
            "yourAnswer": student_answer,
            "correctAnswer": correct_answer,
            "isCorrect": is_correct,
            "explanation": q.get("explanation", ""),
            "topic": q.get("topic", ""),
        })

    final_score = round((correct_count / len(quiz)) * 100) if quiz else 0
    # NOTE: mirrors Flask — the pass threshold below is actually >=50, even
    # though the Flask route's own docstring says ">= 70%". Ported as the
    # code actually behaves, not as documented — a pre-existing Flask
    # doc/code mismatch, not fixed here.
    passed = final_score >= 50

    progress = doc.get("progress", {})
    unlocked = list(doc.get("unlockedLevels", [1]))
    previously_unlocked = set(unlocked)
    next_level = level + 1
    next_level_unlocked = False

    if passed:
        progress.setdefault("passedQuizzes", {})[str(level)] = final_score
        if next_level <= 4 and next_level not in unlocked:
            unlocked.append(next_level)
            next_level_unlocked = next_level not in previously_unlocked
        # NOTE: mirrors Flask — "streakDays" is incremented once per passed
        # quiz, not once per calendar day despite the name. Ported as-is.
        progress["streakDays"] = progress.get("streakDays", 0) + 1

    existing_weak = progress.get("weakTopics", [])
    for wt in wrong_topics:
        if wt not in existing_weak:
            existing_weak.append(wt)
    progress["weakTopics"] = existing_weak[:10]
    progress.setdefault("quizHistory", []).append({
        "level": level,
        "score": final_score,
        "passed": passed,
        "correctCount": correct_count,
        "totalQuestions": len(quiz),
        "weakTopics": wrong_topics,
        "submittedAt": datetime.now(timezone.utc),
        "questions": _sanitize_quiz_for_history(quiz, results_detail),
    })
    progress["quizHistory"] = progress["quizHistory"][-25:]

    doc["progress"] = progress
    doc["unlockedLevels"] = unlocked
    new_overall = _recalculate_progress(doc)
    progress["overallProgress"] = new_overall

    updated_doc = await db["selfLearnerRoadmaps"].find_one_and_update(
        {"_id": roadmap_object_id, "user_id": user_object_id},
        {"$set": {"progress": progress, "unlockedLevels": unlocked, "updated_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )

    logging.info("Quiz graded: roadmap=%s level=%s score=%s%% passed=%s", roadmap_id, level, final_score, passed)
    return {
        "score": final_score,
        "passed": passed,
        "correctCount": correct_count,
        "totalQuestions": len(quiz),
        "nextLevelUnlocked": next_level_unlocked,
        "weakTopics": wrong_topics,
        "results": results_detail,
        "roadmap": serialize_roadmap(updated_doc),
    }


# ── Quiz History ─────────────────────────────────────────────────────────────

@router.get("/{roadmap_id}/quiz/history")
async def get_quiz_history(
    roadmap_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(roadmap_id):
        raise HTTPException(status_code=400, detail="Invalid roadmap id")

    doc = await db["selfLearnerRoadmaps"].find_one(
        {"_id": ObjectId(roadmap_id), "user_id": ObjectId(identity["user_id"])},
        {"progress.quizHistory": 1},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Roadmap not found")

    history = doc.get("progress", {}).get("quizHistory", [])
    return {"history": [serialize_roadmap(item) for item in reversed(history)]}
