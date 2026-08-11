# ============================================================
# ANSWER SHEETS ROUTER
# Ported from routes/institute/answer_routes.py +
# controllers/institute/answer_controller.py
# ============================================================

import asyncio
from datetime import datetime, timezone
from typing import List

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity, get_current_user_and_faculty_details
from app.db.mongodb import get_database
from app.schemas.answers import (
    DeleteFileRequest,
    ManualMarksEntryRequest,
    RenameFileRequest,
    SelfEvaluationRequest,
    UploadAnswerScriptRequest,
)
from app.services.imagekit import delete_imagekit_file, get_imagekit_auth_params
from app.utils.transcript_generation_helper import refresh_transcript_for_exam

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["answers"])


# =====================================================
# UPLOAD ANSWER SCRIPT (metadata only — file already on ImageKit)
# =====================================================

@router.post("/upload-answer-script/{folder_id}")
async def upload_answer_script(
    folder_id: str,
    payload: UploadAnswerScriptRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, faculty_id, error = await get_current_user_and_faculty_details(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)

    if not ObjectId.is_valid(faculty_id):
        raise HTTPException(status_code=400, detail="Invalid faculty_id")
    if not ObjectId.is_valid(folder_id):
        raise HTTPException(status_code=400, detail="Invalid folder ID")

    folder_object_id = ObjectId(folder_id)
    folder = await db["newsavedDocs"].find_one({"_id": folder_object_id})
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    answer_script_url = payload.answer_script_url
    file_id = payload.fileId
    filename = payload.filename

    now = datetime.now(timezone.utc)
    answer_doc = {
        "exam_id": folder_object_id,
        "faculty_id": ObjectId(faculty_id),
        "filename": filename,
        "answer_script_url": answer_script_url,
        "fileId": file_id,
        "created_at": now,
        "updated_at": now,
    }

    result = await db["answerDetails"].insert_one(answer_doc)

    return {
        "success": True,
        "answer_id": str(result.inserted_id),
        "filename": filename,
        "answer_script_url": answer_script_url,
    }


# =====================================================
# RENAME / DELETE FILE
# =====================================================

@router.put("/rename-file")
async def rename_file(payload: RenameFileRequest, db: AsyncIOMotorDatabase = Depends(get_database)):
    answer_id = payload.answer_id
    new_filename = payload.newFilename

    if not answer_id or not ObjectId.is_valid(answer_id):
        raise HTTPException(status_code=400, detail="Invalid answer_id")

    result = await db["answerDetails"].update_one(
        {"_id": ObjectId(answer_id)}, {"$set": {"filename": new_filename, "updated_at": datetime.now(timezone.utc)}}
    )
    if result.modified_count == 1:
        return {"success": True, "message": "Filename updated"}

    raise HTTPException(status_code=500, detail="Update failed")


@router.delete("/delete-file")
async def delete_file(payload: DeleteFileRequest, db: AsyncIOMotorDatabase = Depends(get_database)):
    answer_id = payload.answer_id
    if not answer_id or not ObjectId.is_valid(answer_id):
        raise HTTPException(status_code=400, detail="Invalid answer_id")

    answer_object_id = ObjectId(answer_id)
    answer_doc = await db["answerDetails"].find_one({"_id": answer_object_id})
    if not answer_doc:
        raise HTTPException(status_code=404, detail="Answer script not found")

    deleted_files: List[str] = []
    errors: List[str] = []

    if answer_doc.get("fileId"):
        try:
            await asyncio.to_thread(delete_imagekit_file, answer_doc["fileId"])
            deleted_files.append("answer_script")
        except Exception as e:
            errors.append(str(e))

    if answer_doc.get("evaluated_report_fileId"):
        try:
            await asyncio.to_thread(delete_imagekit_file, answer_doc["evaluated_report_fileId"])
            deleted_files.append("evaluated_report")
        except Exception as e:
            errors.append(str(e))

    await db["answerDetails"].delete_one({"_id": answer_object_id})

    return {"success": True, "deleted_from_imagekit": deleted_files, "errors": errors}


# =====================================================
# LIST ANSWER SCRIPTS
# =====================================================

@router.get("/get-answer-scripts/{folder_id}")
async def get_answer_scripts(folder_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(folder_id):
        raise HTTPException(status_code=400, detail="Invalid exam_id")

    exam_object_id = ObjectId(folder_id)
    answer_scripts = [a async for a in db["answerDetails"].find({"exam_id": exam_object_id})]

    for script in answer_scripts:
        script["_id"] = str(script["_id"])
        script["answer_id"] = script["_id"]
        if script.get("exam_id"):
            script["exam_id"] = str(script["exam_id"])
        if script.get("faculty_id"):
            script["faculty_id"] = str(script["faculty_id"])
        if script.get("subject_id"):
            script["subject_id"] = str(script["subject_id"])

    return {"success": True, "count": len(answer_scripts), "answer_scripts": answer_scripts}


# =====================================================
# IMAGEKIT AUTH
# =====================================================

@router.get("/imagekit-auth")
async def imagekit_auth():
    return get_imagekit_auth_params()


# =====================================================
# SELF EVALUATION
# =====================================================

@router.post("/save-self-evaluation")
async def save_self_evaluation(payload: SelfEvaluationRequest, db: AsyncIOMotorDatabase = Depends(get_database)):
    answer_id = payload.answer_id
    questionwise_marking = payload.questionwise_marking

    if not answer_id or not ObjectId.is_valid(answer_id):
        raise HTTPException(status_code=400, detail="Invalid answer_id")

    total_final_marks = sum(
        (q.get("ai_awarded_marks", 0) + q.get("grace_marks", 0)) for q in questionwise_marking
    )

    answer = await db["answerDetails"].find_one({"_id": ObjectId(answer_id)}, {"exam_id": 1})

    result = await db["answerDetails"].update_one(
        {"_id": ObjectId(answer_id)},
        {"$set": {
            "questionwise_marking": questionwise_marking,
            "total_final_marks": total_final_marks,
            "reviewed_by_professor": True,
            "updated_at": datetime.now(timezone.utc),
        }},
    )

    if result.modified_count == 1:
        if answer and answer.get("exam_id"):
            asyncio.create_task(refresh_transcript_for_exam(db, answer["exam_id"]))
        return {"success": True, "total_final_marks": total_final_marks}

    raise HTTPException(status_code=500, detail="Failed to save evaluation")


# =====================================================
# MANUAL MARKS ENTRY
# =====================================================

@router.post("/manual-marks-entry/{exam_id}")
async def manual_marks_entry(
    exam_id: str,
    payload: ManualMarksEntryRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Lets a faculty member type in marks directly for an exam, with no answer-script
    file involved. Writes the same answerDetails shape the AI-evaluation pipeline
    produces, using a synthetic "MANUAL-<student_id>" filename.
    """
    user, faculty_id, error = await get_current_user_and_faculty_details(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)

    if not ObjectId.is_valid(exam_id):
        raise HTTPException(status_code=400, detail="Invalid exam_id")

    exam = await db["newsavedDocs"].find_one({"_id": ObjectId(exam_id)})
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")

    if exam.get("faculty_id") != ObjectId(faculty_id):
        raise HTTPException(status_code=403, detail="You are not assigned to this exam's subject")

    max_marks = payload.max_marks

    saved = 0
    now = datetime.now(timezone.utc)
    for entry in payload.entries:
        student_id = str(entry.get("student_id", "")).strip()
        try:
            marks = float(entry.get("marks"))
        except (TypeError, ValueError):
            continue
        if not student_id or not (0 <= marks <= max_marks):
            continue

        filename = f"MANUAL-{student_id}"
        await db["answerDetails"].update_one(
            {"exam_id": ObjectId(exam_id), "filename": filename},
            {
                "$set": {
                    "faculty_id": ObjectId(faculty_id),
                    "student_name": student_id,
                    "total_final_marks": marks,
                    "total_ai_marks": marks,
                    "total_max_marks": max_marks,
                    "questionwise_marking": [],
                    "reviewed_by_professor": True,
                    "evaluated_at": now,
                    "updated_at": now,
                },
                "$setOnInsert": {"exam_id": ObjectId(exam_id), "filename": filename, "created_at": now},
            },
            upsert=True,
        )
        saved += 1

    if saved:
        asyncio.create_task(refresh_transcript_for_exam(db, ObjectId(exam_id)))

    return {"success": True, "saved_count": saved}
