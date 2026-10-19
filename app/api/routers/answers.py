# ============================================================
# ANSWER SHEETS ROUTER
# Ported from routes/institute/answer_routes.py +
# controllers/institute/answer_controller.py
# ============================================================

import asyncio
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
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
from app.services.imagekit import delete_imagekit_file, get_imagekit_auth_params, upload_file_to_imagekit
from app.utils.uploads import read_upload_capped
from app.services.exam_paper_lock import lock_exam_for_marks
from app.services.manual_marks import prepare_manual_marks
from app.services.review_marks import validate_review_marks

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["answers"])


@router.post("/answer-script-file/{folder_id}")
async def upload_answer_file(folder_id: str, file: UploadFile = File(...),
                             identity: dict = Depends(get_current_identity),
                             db: AsyncIOMotorDatabase = Depends(get_database)):
    exam = await require_exam_access(identity, db, folder_id)
    content = await read_upload_capped(file)
    filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not filename.lower().endswith(".pdf") or not content.startswith(b"%PDF"):
        raise HTTPException(400, "Upload a PDF answer script")
    upload = await asyncio.to_thread(upload_file_to_imagekit, content, filename,
        f"/answer-scripts/{identity['user_id']}/{folder_id}", ["answer-script"])
    # Only the private-key backend creates receipts; client metadata and tags
    # on direct uploads never establish ownership.
    await db["storageAssets"].insert_one({"_id": upload["file_id"],
        "owner_user_id": ObjectId(identity["user_id"]), "exam_id": exam["_id"],
        "url": upload["url"], "filename": filename, "purpose": "answer-script",
        "answer_id": None, "created_at": datetime.now(timezone.utc)})
    return {"url": upload["url"], "fileId": upload["file_id"], "name": filename}


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

    receipt_query = {"_id": file_id, "owner_user_id": ObjectId(identity["user_id"]),
        "exam_id": folder_object_id, "purpose": "answer-script", "url": answer_script_url,
        "filename": filename}
    receipt = await db["storageAssets"].find_one(receipt_query)
    if not receipt:
        raise HTTPException(404, "Verified upload not found. Upload this answer script again.")
    candidate = ObjectId()
    claim = await db["storageAssets"].update_one({**receipt_query, "answer_id": None},
        {"$set": {"answer_id": candidate}})
    receipt = await db["storageAssets"].find_one(receipt_query)
    answer_id = receipt["answer_id"]
    existing = await db["answerDetails"].find_one({"_id": answer_id})
    if not claim.matched_count and not existing:
        raise HTTPException(409, "This upload was already saved or its save is incomplete. Refresh; upload again if the answer was deleted.")

    now = datetime.now(timezone.utc)
    answer_doc = {
        "_id": answer_id,
        "exam_id": folder_object_id,
        "faculty_id": ObjectId(faculty_id),
        "filename": filename,
        "answer_script_url": answer_script_url,
        "fileId": file_id,
        "verified_storage_ids": [file_id],
        "created_at": now,
        "updated_at": now,
    }

    from pymongo.errors import DuplicateKeyError
    if not existing:
        try:
            await db["answerDetails"].insert_one(answer_doc)
        except DuplicateKeyError:
            pass  # Concurrent retry of the same server-issued receipt.

    return {
        "success": True,
        "answer_id": str(answer_id),
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

    # Delete the authorized record first. A failed DB write must never leave a
    # live record pointing to a file we already destroyed.
    result = await db["answerDetails"].delete_one({"_id": answer_object_id, "exam_id": answer_doc["exam_id"]})
    if result.deleted_count != 1:
        raise HTTPException(409, "Answer changed or was already deleted")
    deleted_files, errors, retained_files = [], [], []
    verified = answer_doc.get("verified_storage_ids") or []
    for field, label in (("fileId", "answer_script"), ("evaluated_report_fileId", "evaluated_report"),
                         ("transcript_pdf_fileId", "transcript")):
        file_id = answer_doc.get(field)
        if not file_id:
            continue
        if file_id not in verified:
            retained_files.append(label)
            continue
        try:
            await asyncio.to_thread(delete_imagekit_file, file_id)
            deleted_files.append(label)
        except Exception as e:
            errors.append(str(e))
    return {"success": True, "deleted_from_imagekit": deleted_files, "errors": errors,
            "retained_unverified_files": retained_files}


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

    await lock_exam_for_marks(db, ObjectId(exam_id), exam.get("question_paper") or {},
                              rubric_revision=exam.get("rubric_revision"))
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
