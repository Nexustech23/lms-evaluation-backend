# ============================================================
# AI TUTOR "V1" ROUTER (endpoint-parity port, dead code)
# Ported from routes/institute/ai_tutor_routes.py +
# controllers/institute/ai_tutor_controller.py.
#
# This is a second, MongoDB-backed AI Tutor implementation in the Flask
# codebase that the frontend never calls — the frontend's hardcoded
# absolute URLs point at homework_help_controller.py /
# notes_generate_controller.py instead (already ported as
# app/api/routers/ai_tutor.py, mounted at /api/ai-tutor/...). Ported here
# anyway for literal endpoint-for-endpoint parity, at root paths matching
# Flask's actual (unprefixed) blueprint mount.
#
# Deviation from Flask: in the Flask source, `@jwt_required()` is placed
# ABOVE `@blueprint.route(...)` on every route in this file — a decorator-
# order bug that means the blueprint's url_map still references the
# *unwrapped* view function, so jwt_required() never actually runs and
# these 7 endpoints are effectively unauthenticated in Flask right now.
# This port intentionally requires auth (dependencies=[Depends(...)])
# instead of reproducing that hole, matching this codebase's own
# established precedent (see app/api/routers/ai_tutor.py's note that it
# added auth Flask didn't have).
# ============================================================

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity
from app.db.mongodb import get_database
from app.models.ai_tutor_v1 import create_ai_tutor_document, serialize_ai_tutor
from app.schemas.ai_tutor_v1 import AiTutorV1UpdateRequest
from app.services.ai_tutor_v1_ai import (
    UPLOAD_FOLDER,
    allowed_file,
    extract_text_with_gemini,
    generate_homework_pdf,
    generate_homework_with_claude,
    generate_notes_pdf,
    generate_notes_with_claude,
    secure_filename,
)
from app.services.job_store import get_job, set_job, update_job

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["ai-tutor-v1"])

# NOTE: mirrors Flask exactly — both homework and notes jobs share this one
# Redis key prefix (there's no separate "notes" status endpoint; polling
# always goes through GET /homework-help/status/{jobId} regardless of which
# job type it is).
JOB_PREFIX = "homework_job:"

MAX_FILE_SIZE = 10 * 1024 * 1024


# ============================================================
# BACKGROUND JOBS
# ============================================================

async def _run_homework_job(
    job_id: str, document_id: ObjectId, prompt: str, homework_type: str, response_style: str, file_path: Optional[str],
) -> None:
    db = get_database()
    try:
        extracted_text = ""
        gemini_response: Dict[str, Any] = {}

        if file_path:
            await update_job(JOB_PREFIX, job_id, {"status": "processing", "step": "extracting_homework"})
            gemini_response = await asyncio.to_thread(extract_text_with_gemini, file_path)
            extracted_text = gemini_response["extracted_text"]
            if not extracted_text.strip():
                raise ValueError("No text extracted from file.")

            await db["ai_tutor"].update_one(
                {"_id": document_id},
                {"$set": {
                    "extracted_text": extracted_text, "status": "extracted",
                    "token_usage.gemini": gemini_response["token_usage"],
                    "updated_at": datetime.now(timezone.utc),
                }},
            )

        await update_job(JOB_PREFIX, job_id, {"step": "generating_solution"})
        claude_response = await asyncio.to_thread(
            generate_homework_with_claude, prompt, extracted_text, homework_type, response_style
        )

        gemini_tokens = gemini_response.get("token_usage", {"prompt_tokens": 0, "completion_tokens": 0}) if file_path else {"prompt_tokens": 0, "completion_tokens": 0}
        total_input_tokens = gemini_tokens.get("prompt_tokens", 0) + claude_response["token_usage"].get("input_tokens", 0)
        total_output_tokens = gemini_tokens.get("completion_tokens", 0) + claude_response["token_usage"].get("output_tokens", 0)
        total_tokens = total_input_tokens + total_output_tokens

        generated_content = claude_response["generated_content"]
        if not generated_content.strip():
            raise ValueError("Claude returned empty homework response.")

        await db["ai_tutor"].update_one(
            {"_id": document_id},
            {"$set": {
                "generated_content": generated_content, "status": "generated",
                "token_usage.claude": claude_response["token_usage"],
                "token_usage.total_input_tokens": total_input_tokens,
                "token_usage.total_output_tokens": total_output_tokens,
                "token_usage.total_tokens": total_tokens,
                "updated_at": datetime.now(timezone.utc),
            }},
        )

        await update_job(JOB_PREFIX, job_id, {"step": "building_pdf"})
        pdf_result = await asyncio.to_thread(
            generate_homework_pdf, str(document_id), prompt, homework_type, response_style, generated_content
        )
        if not pdf_result.get("pdf_path"):
            raise ValueError("PDF generation failed.")

        await db["ai_tutor"].update_one(
            {"_id": document_id},
            {"$set": {
                "pdf_url": pdf_result["pdf_path"].replace("\\", "/"),
                "pdf_filename": pdf_result["pdf_filename"],
                "status": "completed",
                "updated_at": datetime.now(timezone.utc),
            }},
        )

        updated_doc = await db["ai_tutor"].find_one({"_id": document_id})

        if file_path and os.path.exists(file_path):
            os.remove(file_path)

        await set_job(JOB_PREFIX, job_id, {"status": "completed", "step": "done", "data": serialize_ai_tutor(updated_doc)})

    except Exception as e:
        logging.error(str(e))
        await set_job(JOB_PREFIX, job_id, {"status": "failed", "error": str(e)})


async def _run_notes_job(
    job_id: str, document_id: ObjectId, prompt: str, notes_type: str, notes_length: str, file_path: Optional[str],
) -> None:
    db = get_database()
    try:
        extracted_text = ""
        gemini_response: Dict[str, Any] = {}

        if file_path:
            await update_job(JOB_PREFIX, job_id, {"status": "processing", "step": "extracting_notes"})
            gemini_response = await asyncio.to_thread(extract_text_with_gemini, file_path)
            extracted_text = gemini_response["extracted_text"]
            if not extracted_text.strip():
                raise ValueError("No text extracted from file.")

            await db["ai_tutor"].update_one(
                {"_id": document_id},
                {"$set": {
                    "extracted_text": extracted_text, "status": "extracted",
                    "token_usage.gemini": gemini_response["token_usage"],
                    "updated_at": datetime.now(timezone.utc),
                }},
            )

        await update_job(JOB_PREFIX, job_id, {"step": "generating_notes"})
        claude_response = await asyncio.to_thread(
            generate_notes_with_claude, prompt, extracted_text, notes_type, notes_length
        )

        gemini_tokens = gemini_response.get("token_usage", {"prompt_tokens": 0, "completion_tokens": 0}) if file_path else {"prompt_tokens": 0, "completion_tokens": 0}
        total_input_tokens = gemini_tokens.get("prompt_tokens", 0) + claude_response["token_usage"].get("input_tokens", 0)
        total_output_tokens = gemini_tokens.get("completion_tokens", 0) + claude_response["token_usage"].get("output_tokens", 0)
        total_tokens = total_input_tokens + total_output_tokens

        generated_content = claude_response["generated_content"]
        if not generated_content.strip():
            raise ValueError("Claude returned empty notes response.")

        await db["ai_tutor"].update_one(
            {"_id": document_id},
            {"$set": {
                "generated_content": generated_content, "status": "generated",
                "token_usage.claude": claude_response["token_usage"],
                "token_usage.total_input_tokens": total_input_tokens,
                "token_usage.total_output_tokens": total_output_tokens,
                "token_usage.total_tokens": total_tokens,
                "updated_at": datetime.now(timezone.utc),
            }},
        )

        await update_job(JOB_PREFIX, job_id, {"step": "building_pdf"})
        pdf_result = await asyncio.to_thread(
            generate_notes_pdf, str(document_id), prompt, notes_type, notes_length, generated_content
        )
        if not pdf_result.get("pdf_path"):
            raise ValueError("Notes PDF generation failed.")

        await db["ai_tutor"].update_one(
            {"_id": document_id},
            {"$set": {
                "pdf_url": pdf_result["pdf_path"].replace("\\", "/"),
                "pdf_filename": pdf_result["pdf_filename"],
                "status": "completed",
                "updated_at": datetime.now(timezone.utc),
            }},
        )

        updated_doc = await db["ai_tutor"].find_one({"_id": document_id})

        if file_path and os.path.exists(file_path):
            os.remove(file_path)

        await set_job(JOB_PREFIX, job_id, {"status": "completed", "step": "done", "data": serialize_ai_tutor(updated_doc)})

    except Exception as e:
        logging.error(str(e))
        await set_job(JOB_PREFIX, job_id, {"status": "failed", "error": str(e)})


# ============================================================
# HOMEWORK ROUTES
# ============================================================

@router.post("/homework-help")
async def create_homework(
    background_tasks: BackgroundTasks,
    prompt: str = Form(""),
    homeworkType: str = Form(""),
    responseStyle: str = Form(""),
    file: Optional[UploadFile] = File(None),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    prompt = prompt.strip()
    homework_type = homeworkType.strip()
    response_style = responseStyle.strip()

    if not prompt and not file:
        raise HTTPException(status_code=400, detail="Prompt or file is required.")

    source_file = None
    file_path = None

    if file and file.filename:
        if not allowed_file(file.filename):
            raise HTTPException(status_code=400, detail="Invalid file type.")

        content = await file.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="File size exceeds 10MB limit.")

        unique_filename = f"{uuid.uuid4()}_{secure_filename(file.filename)}"
        file_path = os.path.join(UPLOAD_FOLDER, unique_filename)
        with open(file_path, "wb") as f:
            f.write(content)

        source_file = {"filename": unique_filename, "path": file_path}

    ai_tutor_doc = create_ai_tutor_document({
        "feature_type": "homework", "prompt": prompt, "homework_type": homework_type,
        "response_style": response_style, "source_file": source_file,
    })
    result = await db["ai_tutor"].insert_one(ai_tutor_doc)
    document_id = result.inserted_id

    job_id = str(uuid.uuid4())
    await set_job(JOB_PREFIX, job_id, {"status": "queued", "step": "starting", "job_id": job_id})

    background_tasks.add_task(_run_homework_job, job_id, document_id, prompt, homework_type, response_style, file_path)

    return JSONResponse(status_code=202, content={"success": True, "jobId": job_id, "message": "Homework generation started."})


@router.get("/homework-help/status/{job_id}")
async def get_homework_job_status(job_id: str):
    job = await get_job(JOB_PREFIX, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


# ============================================================
# NOTES ROUTES
# ============================================================

@router.post("/generate-notes")
async def create_notes(
    background_tasks: BackgroundTasks,
    prompt: str = Form(""),
    notesType: str = Form(""),
    notesLength: str = Form(""),
    file: Optional[UploadFile] = File(None),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    prompt = prompt.strip()
    notes_type = notesType.strip()
    notes_length = notesLength.strip()

    if not prompt and not file:
        raise HTTPException(status_code=400, detail="Prompt or file is required.")

    source_file = None
    file_path = None

    if file and file.filename:
        if not allowed_file(file.filename):
            raise HTTPException(status_code=400, detail=f"File type not allowed. Allowed: {{'pdf', 'png', 'jpg', 'jpeg'}}")

        content = await file.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="File size exceeds 10MB limit.")

        unique_filename = f"{uuid.uuid4()}_{secure_filename(file.filename)}"
        file_path = os.path.join(UPLOAD_FOLDER, unique_filename)
        with open(file_path, "wb") as f:
            f.write(content)

        source_file = {"filename": unique_filename, "path": file_path}

    ai_tutor_doc = create_ai_tutor_document({
        "feature_type": "notes", "prompt": prompt, "notes_type": notes_type,
        "notes_length": notes_length, "source_file": source_file,
    })
    result = await db["ai_tutor"].insert_one(ai_tutor_doc)
    document_id = result.inserted_id

    job_id = str(uuid.uuid4())
    await set_job(JOB_PREFIX, job_id, {"status": "queued", "step": "starting", "job_id": job_id})

    background_tasks.add_task(_run_notes_job, job_id, document_id, prompt, notes_type, notes_length, file_path)

    return JSONResponse(status_code=202, content={"success": True, "jobId": job_id, "message": "Notes generation started."})


# ============================================================
# CRUD ROUTES
# ============================================================
# NOTE: mirrors Flask — no user/institute ownership scoping on any of these
# 4 routes (the whole `ai_tutor` collection is a flat, un-scoped list).
# Ported as-is.

@router.get("/get-all")
async def get_all_ai_tutors(db: AsyncIOMotorDatabase = Depends(get_database)):
    docs = [d async for d in db["ai_tutor"].find().sort("created_at", -1)]
    serialized = [serialize_ai_tutor(d) for d in docs]
    return {"success": True, "count": len(serialized), "data": serialized}


@router.get("/get/{document_id}")
async def get_single_ai_tutor(document_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(document_id):
        raise HTTPException(status_code=400, detail="Invalid document_id")

    document = await db["ai_tutor"].find_one({"_id": ObjectId(document_id)})
    if not document:
        raise HTTPException(status_code=404, detail="Record not found.")

    return {"success": True, "data": serialize_ai_tutor(document)}


@router.put("/update/{document_id}")
async def update_ai_tutor(
    document_id: str, payload: AiTutorV1UpdateRequest, db: AsyncIOMotorDatabase = Depends(get_database)
):
    if not ObjectId.is_valid(document_id):
        raise HTTPException(status_code=400, detail="Invalid document_id")

    update_data = payload.model_dump(exclude_unset=True)
    update_data["updated_at"] = datetime.now(timezone.utc)

    result = await db["ai_tutor"].update_one({"_id": ObjectId(document_id)}, {"$set": update_data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Record not found.")

    updated_doc = await db["ai_tutor"].find_one({"_id": ObjectId(document_id)})
    return {"success": True, "message": "Record updated successfully.", "data": serialize_ai_tutor(updated_doc)}


@router.delete("/delete/{document_id}")
async def delete_ai_tutor(document_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(document_id):
        raise HTTPException(status_code=400, detail="Invalid document_id")

    result = await db["ai_tutor"].delete_one({"_id": ObjectId(document_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Record not found.")

    return {"success": True, "message": "Record deleted successfully."}
