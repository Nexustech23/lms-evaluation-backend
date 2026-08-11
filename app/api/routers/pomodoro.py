# ============================================================
# POMODORO ROUTER
# Ported from routes/self_learner/pomodoro_routes.py +
# controllers/self_learner/pomodoro_controller.py.
#
# Flask's blueprint is mounted at url_prefix="/api/pomodoro" (unlike most
# other blueprints, which are mounted at root) — mirrored here with
# prefix="/api/pomodoro", matching how app/api/routers/ai_tutor.py already
# handles the same "/api/<feature>"-prefixed self-learner blueprints.
#
# Async generation uses the existing Redis-backed job_store.py +
# BackgroundTasks pattern (matching ai_tutor.py/question_paper.py) instead
# of Flask's raw threading.Thread + in-memory dict, per the migration plan.
# Job status values ("pending"/"processing"/"done"/"error") are kept as
# Flask's own strings (not ai_tutor.py's "completed"/"failed") since this
# is what the self-learner frontend's poller already expects.
# ============================================================

import asyncio
import base64
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity
from app.db.mongodb import get_database
from app.models.pomodoro import create_ai_assisted_document, create_ai_driven_document, create_custom_document, serialize_session
from app.schemas.pomodoro import (
    AiDrivenGenerateRequest,
    CompleteSessionRequest,
    CustomCreateRequest,
    SubmitTestRequest,
)
from app.services.imagekit import upload_file_to_imagekit
from app.services.job_store import get_job, set_job, update_job
from app.services.pomodoro_ai import (
    evaluate,
    extract_and_section,
    extract_uploaded_document_text,
    extract_written_answer,
    generate_notes,
    generate_test,
    safe_serialise,
)

router = APIRouter(prefix="/api/pomodoro", dependencies=[Depends(get_current_identity)], tags=["pomodoro"])

POMODORO_JOB_PREFIX = "pomodoro_job:"


# ============================================================
# BACKGROUND JOBS
# ============================================================

async def _run_ai_pipeline(job_id: str, session_id: str, sections_raw: list, config: dict) -> None:
    db = get_database()
    try:
        await update_job(POMODORO_JOB_PREFIX, job_id, {"status": "processing", "step": "generating_tests"})

        test_format = config["test_format"]
        test_duration = config["test_duration_mins"]

        # Run sequentially to stay within Claude/Gemini API rate limits (matches Flask).
        sections = []
        for idx, s in enumerate(sections_raw):
            test, _usage = await asyncio.to_thread(
                generate_test, s["content"], s["title"], test_format, test_duration, idx
            )
            sections.append({
                "index": idx,
                "title": s["title"],
                "content": s["content"],
                "study_duration_mins": s.get("study_duration_mins", 25),
                "submitted_answers": [],
                "test": test,
            })

        now = datetime.now(timezone.utc)
        await db["pomodoroSessions"].update_one(
            {"_id": ObjectId(session_id)},
            {"$set": {"sections": sections, "status": "active", "started_at": now, "updated_at": now}},
        )

        await set_job(POMODORO_JOB_PREFIX, job_id, {"status": "done", "session_id": session_id})
        logging.info("[pomodoro:%s] pipeline done — session=%s", job_id, session_id)

    except Exception as e:
        logging.error("[pomodoro:%s] pipeline error: %s", job_id, e, exc_info=True)
        await set_job(POMODORO_JOB_PREFIX, job_id, {"status": "error", "message": str(e)})
        await db["pomodoroSessions"].update_one(
            {"_id": ObjectId(session_id)},
            {"$set": {"status": "interrupted", "updated_at": datetime.now(timezone.utc)}},
        )


async def _run_ai_driven_job(job_id: str, session_id: str, prompt: str, config: dict) -> None:
    try:
        sections_raw, _usage = await asyncio.to_thread(generate_notes, prompt, config)
        await _run_ai_pipeline(job_id, session_id, sections_raw, config)
    except Exception as e:
        logging.error("[pomodoro:%s] AI-driven generation failed: %s", job_id, e, exc_info=True)
        await set_job(POMODORO_JOB_PREFIX, job_id, {"status": "error", "message": str(e)})


async def _run_ai_assisted_job(job_id: str, session_id: str, content: bytes, filename: str, config: dict) -> None:
    try:
        extracted_text, _usage = await asyncio.to_thread(extract_uploaded_document_text, content, filename)
        sections_raw, _usage2 = await asyncio.to_thread(extract_and_section, extracted_text, config)
        await _run_ai_pipeline(job_id, session_id, sections_raw, config)
    except Exception as e:
        logging.error("[pomodoro:%s] AI-assisted generation failed: %s", job_id, e, exc_info=True)
        await set_job(POMODORO_JOB_PREFIX, job_id, {"status": "error", "message": str(e)})


# ============================================================
# AI-DRIVEN
# ============================================================

@router.post("/ai-driven/generate")
async def ai_driven_generate(
    background_tasks: BackgroundTasks,
    payload: AiDrivenGenerateRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    prompt = payload.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    config = {
        "total_study_time_mins": payload.total_study_time,
        "revision_time_mins": payload.revision_time,
        "test_duration_mins": payload.test_duration,
        "test_format": payload.test_format,
        "num_tests": payload.num_tests,
    }

    doc = create_ai_driven_document(identity["user_id"], {**payload.model_dump(), **config})
    result = await db["pomodoroSessions"].insert_one(doc)
    session_id = str(result.inserted_id)

    job_id = str(uuid.uuid4())
    await set_job(POMODORO_JOB_PREFIX, job_id, {
        "status": "pending", "session_id": session_id, "user_id": identity["user_id"],
    })

    background_tasks.add_task(_run_ai_driven_job, job_id, session_id, prompt, config)

    return JSONResponse(status_code=202, content={"job_id": job_id, "message": "Generation started"})


# ============================================================
# AI-ASSISTED
# ============================================================

@router.post("/ai-assisted/upload")
async def ai_assisted_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    # Previously str Form fields manually coerced via a safe_int() helper
    # that silently fell back to a default on bad input (e.g.
    # total_study_time="abc" -> 60) instead of rejecting it — typing these
    # as int lets FastAPI do the coercion and reject invalid input with a
    # 422 instead, an intentional behavior change (silent fallback on bad
    # input was a latent bug, not a feature).
    total_study_time: int = Form(60),
    revision_time: int = Form(10),
    test_duration: int = Form(5),
    test_format: str = Form("mcq"),
    num_tests: int = Form(3),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    config = {
        "total_study_time_mins": total_study_time,
        "revision_time_mins": revision_time,
        "test_duration_mins": test_duration,
        "test_format": test_format or "mcq",
        "num_tests": num_tests,
    }

    content = await file.read()
    fname = file.filename or "upload.pdf"

    upload = await asyncio.to_thread(upload_file_to_imagekit, content, fname, "/pomodoro-uploads", [])
    file_url = upload["url"]

    form_data = {
        "title": title, "total_study_time": total_study_time, "revision_time": revision_time,
        "test_duration": test_duration, "test_format": test_format, "num_tests": num_tests,
    }
    doc = create_ai_assisted_document(identity["user_id"], {**form_data, **config}, file_url, fname)
    result = await db["pomodoroSessions"].insert_one(doc)
    session_id = str(result.inserted_id)

    job_id = str(uuid.uuid4())
    await set_job(POMODORO_JOB_PREFIX, job_id, {
        "status": "pending", "session_id": session_id, "user_id": identity["user_id"],
    })

    background_tasks.add_task(_run_ai_assisted_job, job_id, session_id, content, fname, config)

    return JSONResponse(status_code=202, content={"job_id": job_id, "message": "Upload received, processing started"})


# ============================================================
# CUSTOM
# ============================================================

@router.post("/custom/create")
async def custom_create(
    payload: CustomCreateRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    doc = create_custom_document(identity["user_id"], payload.model_dump())
    result = await db["pomodoroSessions"].insert_one(doc)
    return JSONResponse(
        status_code=201,
        content={"session_id": str(result.inserted_id), "message": "Custom session created"},
    )


# ============================================================
# SHARED — JOB POLLING
# ============================================================

@router.get("/job/{job_id}")
async def get_job_status(job_id: str, identity: dict = Depends(get_current_identity)):
    job = await get_job(POMODORO_JOB_PREFIX, job_id)
    if not job or job.get("user_id") != identity["user_id"]:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ============================================================
# SHARED — SESSION CRUD
# ============================================================

@router.get("/session/{session_id}")
async def get_session(
    session_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")

    doc = await db["pomodoroSessions"].find_one(
        {"_id": ObjectId(session_id), "user_id": ObjectId(identity["user_id"])}
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Session not found")

    return serialize_session(doc)


@router.post("/session/{session_id}/submit-test")
async def submit_test(
    session_id: str,
    payload: SubmitTestRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")

    section_index = payload.section_index
    answers = payload.answers

    doc = await db["pomodoroSessions"].find_one(
        {"_id": ObjectId(session_id), "user_id": ObjectId(identity["user_id"])}
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Session not found")

    sections = doc.get("sections", [])
    if section_index >= len(sections):
        raise HTTPException(status_code=400, detail="Invalid section index")

    section = sections[section_index]
    questions = section.get("test", {}).get("questions", [])

    graded_answers = []
    marks_obtained = 0
    marks_possible = 0

    for q in questions:
        qno = q.get("question_no")
        correct = (q.get("correct_answer") or "").strip().lower()
        q_marks = q.get("marks", 2)
        marks_possible += q_marks

        user_ans_obj = next((a for a in answers if a.get("question_no") == qno), None)
        user_ans_text = (user_ans_obj.get("user_answer", "") if user_ans_obj else "").strip()

        # If the student uploaded a photo of a handwritten answer, OCR it via
        # Gemini vision and use the extracted text for grading instead.
        image_data = user_ans_obj.get("answer_image_data") if user_ans_obj else None
        if image_data and "," in image_data:
            try:
                header, encoded = image_data.split(",", 1)
                file_mime = header.split(";")[0].split(":")[1]
                file_bytes = base64.b64decode(encoded)
                extracted_text = await asyncio.to_thread(extract_written_answer, file_bytes, file_mime)
                if extracted_text:
                    logging.info("Extracted written answer for Q%s: %s", qno, extracted_text)
                    user_ans_text = extracted_text
            except Exception as ex:
                logging.error("Failed to extract written answer for Q%s via Gemini Vision: %s", qno, ex)

        # NOTE: mirrors Flask — MCQ and written questions both use naive
        # case-insensitive exact-match grading here; the richer holistic
        # feedback happens separately in get_evaluation below. A Flask
        # comment mentioned an MCQ "letter fallback" but it was a no-op
        # there too (dead code) — not ported.
        user_ans_clean = user_ans_text.strip().lower()
        correct_clean = correct.strip().lower()
        is_correct = user_ans_clean == correct_clean

        awarded = q_marks if is_correct else 0
        marks_obtained += awarded

        graded_answers.append({
            "question_no": qno,
            "user_answer": user_ans_text,
            "correct_answer": q.get("correct_answer"),
            "is_correct": is_correct,
            "marks_awarded": awarded,
        })

    score = round((marks_obtained / marks_possible * 100), 1) if marks_possible > 0 else 0

    await db["pomodoroSessions"].update_one(
        {"_id": ObjectId(session_id)},
        {"$set": {
            f"sections.{section_index}.submitted_answers": graded_answers,
            f"sections.{section_index}.score": score,
            f"sections.{section_index}.marks_obtained": marks_obtained,
            f"sections.{section_index}.marks_possible": marks_possible,
            "updated_at": datetime.now(timezone.utc),
        }},
    )

    return {
        "section_index": section_index,
        "score": score,
        "marks_obtained": marks_obtained,
        "marks_possible": marks_possible,
        "graded_answers": graded_answers,
    }


@router.get("/session/{session_id}/evaluation")
async def get_evaluation(
    session_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")

    doc = await db["pomodoroSessions"].find_one(
        {"_id": ObjectId(session_id), "user_id": ObjectId(identity["user_id"])}
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Session not found")

    # NOTE: mirrors Flask — this cached-or-generate check isn't atomic (no
    # findAndModify/lock), so two concurrent first-time requests could both
    # trigger AI evaluation before either write lands. Ported as-is.
    if doc.get("evaluation"):
        return {"evaluation": safe_serialise(doc["evaluation"])}

    try:
        evaluation, _usage = await asyncio.to_thread(evaluate, doc)
    except Exception as e:
        logging.error("get_evaluation failed for session %s: %s", session_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Evaluation failed: {e}")

    now = datetime.now(timezone.utc)
    await db["pomodoroSessions"].update_one(
        {"_id": ObjectId(session_id)},
        {"$set": {"evaluation": evaluation, "status": "completed", "completed_at": now, "updated_at": now}},
    )

    return {"evaluation": evaluation}


@router.patch("/session/{session_id}/complete")
async def complete_session(
    session_id: str,
    payload: CompleteSessionRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")

    status = payload.status
    total_focused = payload.total_focused_mins

    result = await db["pomodoroSessions"].update_one(
        {"_id": ObjectId(session_id), "user_id": ObjectId(identity["user_id"])},
        {"$set": {
            "status": status,
            "completed_at": datetime.now(timezone.utc),
            "total_focused_mins": total_focused,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Session not found")

    return {"message": f"Session marked as {status}"}


@router.delete("/session/{session_id}")
async def delete_session(
    session_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")

    result = await db["pomodoroSessions"].delete_one(
        {"_id": ObjectId(session_id), "user_id": ObjectId(identity["user_id"])}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Session not found")

    return {"message": "Session deleted"}


# ============================================================
# HISTORY
# ============================================================

@router.get("/history")
async def get_history(
    page: int = Query(1),
    limit: int = Query(10),
    mode: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    page = max(1, page)
    limit = min(50, limit)

    filters: Dict[str, Any] = {"user_id": ObjectId(identity["user_id"])}
    if mode in {"ai-driven", "ai-assisted", "custom"}:
        filters["mode"] = mode
    if status in {"active", "completed", "interrupted"}:
        filters["status"] = status

    total = await db["pomodoroSessions"].count_documents(filters)
    cursor = (
        db["pomodoroSessions"]
        .find(filters, {"sections.content": 0, "sections.test.questions": 0})
        .sort("created_at", -1)
        .skip((page - 1) * limit)
        .limit(limit)
    )
    docs = [d async for d in cursor]

    return {
        "sessions": [serialize_session(d) for d in docs],
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": ((total + limit - 1) // limit) if limit > 0 else 1,
    }
