# ============================================================
# ANSWER SHEETS ROUTER
# Ported from routes/institute/answer_routes.py +
# controllers/institute/answer_controller.py
# ============================================================

import asyncio
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity
from app.api.answer_access import require_answer_access, require_exam_access
from app.core.queue import enqueue
from app.db.mongodb import get_database
from app.schemas.answers import (
    DeleteFileRequest,
    ManualMarksEntryRequest,
    RenameFileRequest,
    SelfEvaluationRequest,
    UploadAnswerScriptRequest,
)
from app.services.imagekit import delete_imagekit_file, get_imagekit_auth_params
from app.services.exam_paper_lock import lock_exam_for_marks
from app.services.manual_marks import prepare_manual_marks
from app.services.review_marks import validate_review_marks

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
    folder = await require_exam_access(identity, db, folder_id)
    folder_object_id = folder["_id"]
    faculty_id = ObjectId(str(folder["faculty_id"]))

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
async def rename_file(
    payload: RenameFileRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    answer_id = payload.answer_id
    new_filename = payload.newFilename

    answer = await require_answer_access(identity, db, answer_id)

    result = await db["answerDetails"].update_one(
        {"_id": answer["_id"], "exam_id": answer["exam_id"]},
        {"$set": {"filename": new_filename, "updated_at": datetime.now(timezone.utc)}}
    )
    if result.modified_count == 1:
        return {"success": True, "message": "Filename updated"}

    raise HTTPException(status_code=500, detail="Update failed")


@router.delete("/delete-file")
async def delete_file(
    payload: DeleteFileRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    answer_id = payload.answer_id
    answer_doc = await require_answer_access(identity, db, answer_id)
    answer_object_id = answer_doc["_id"]

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

    await db["answerDetails"].delete_one({"_id": answer_object_id, "exam_id": answer_doc["exam_id"]})

    return {"success": True, "deleted_from_imagekit": deleted_files, "errors": errors}


# =====================================================
# LIST ANSWER SCRIPTS
# =====================================================

@router.get("/get-answer-scripts/{folder_id}")
async def get_answer_scripts(
    folder_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    exam = await require_exam_access(identity, db, folder_id, allow_institute_read=True)
    exam_object_id = exam["_id"]
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
async def save_self_evaluation(
    background_tasks: BackgroundTasks,
    payload: SelfEvaluationRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    answer_id = payload.answer_id
    answer = await require_answer_access(identity, db, answer_id)
    try:
        questionwise_marking = validate_review_marks(payload.questionwise_marking, answer)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    total_final_marks = sum(q["final_marks"] for q in questionwise_marking)

    await lock_exam_for_marks(db, answer["exam_id"])

    result = await db["answerDetails"].update_one(
        {"_id": answer["_id"], "exam_id": answer["exam_id"]},
        {"$set": {
            "questionwise_marking": questionwise_marking,
            "total_final_marks": total_final_marks,
            "reviewed_by_professor": True,
            "updated_at": datetime.now(timezone.utc),
        }},
    )

    if result.modified_count == 1:
        if answer and answer.get("exam_id"):
            await enqueue("run_refresh_transcript", str(answer["exam_id"]),
                          background_tasks=background_tasks)
        return {"success": True, "total_final_marks": total_final_marks}

    raise HTTPException(status_code=500, detail="Failed to save evaluation")


# =====================================================
# MANUAL MARKS ENTRY
# =====================================================

@router.post("/manual-marks-entry/{exam_id}")
async def manual_marks_entry(
    exam_id: str,
    background_tasks: BackgroundTasks,
    payload: ManualMarksEntryRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Lets a faculty member type in marks directly for an exam, with no answer-script
    file involved. If the exam has a saved rubric (evaluationDetails), marks are
    entered per question and each question's marks are split across its COs in
    proportion to that CO's share of the question's max marks — producing the same
    questionwise_marking shape the AI-grading/review pipeline writes (via the same
    validate_questionwise validator), so CO/PO attainment counts these students
    correctly instead of silently treating them as 0% on every CO. Exams with no
    saved rubric fall back to a single total-marks entry with no CO breakdown, same
    as before. Either way, writes to answerDetails with a synthetic
    "MANUAL-<student_id>" filename.
    """
    exam = await require_exam_access(identity, db, exam_id)
    faculty_id = ObjectId(str(exam["faculty_id"]))

    evaluation = await db["evaluationDetails"].find_one({"exam_id": ObjectId(exam_id)})
    try:
        entries = prepare_manual_marks(payload, evaluation)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    await lock_exam_for_marks(db, ObjectId(exam_id), exam.get("question_paper") or {})
    now = datetime.now(timezone.utc)
    saved = 0
    for entry in entries:
        filename = f"MANUAL-{entry['student_id']}"
        await db["answerDetails"].update_one(
            {"exam_id": ObjectId(exam_id), "filename": filename},
            {"$set": {
                **entry, "faculty_id": faculty_id, "student_name": entry["student_id"],
                "reviewed_by_professor": True, "evaluated_at": now, "updated_at": now,
            }, "$setOnInsert": {
                "exam_id": ObjectId(exam_id), "filename": filename, "created_at": now,
            }},
            upsert=True,
        )
        saved += 1

    if saved:
        await enqueue("run_refresh_transcript", str(exam_id),
                      background_tasks=background_tasks)

    return {"success": True, "saved_count": saved}
