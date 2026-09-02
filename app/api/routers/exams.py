# ============================================================
# EXAMS / FOLDERS ROUTER
# Ported from routes/institute/exam_routes.py +
# controllers/institute/examfolder_controller.py
# ============================================================

import asyncio
import concurrent.futures
import io
import json
import logging
import math
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity, get_current_user_and_faculty_details
from app.core.queue import enqueue
from app.core.rate_limit import ai_rate_limit
from app.db.mongodb import get_database
from app.models.exam import create_exam_document
from app.schemas.exams import (
    DeleteFolderRequest,
    RenameFolderRequest,
    SetArchiveStatusRequest,
    UploadQuestionPaperRequest,
)
from app.utils.net import SsrfError, safe_get
from app.utils.query import search_regex
from app.utils.token_usage import check_institute_token_budget

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["exams"])


async def _require_faculty(identity: dict, db: AsyncIOMotorDatabase):
    user, faculty_id, error = await get_current_user_and_faculty_details(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)
    return user, faculty_id


# =====================================================
# CREATE FOLDER
# =====================================================

@router.post("/createSaveFolder")
async def create_folder(
    request: Request,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    data = dict(await request.form())
    user, faculty_id = await _require_faculty(identity, db)

    covered_cos = data.get("covered_cos")
    if isinstance(covered_cos, str):
        try:
            covered_cos = json.loads(covered_cos)
        except Exception:
            covered_cos = covered_cos.split(",")

    exam_payload = {
        "folder_name": data.get("folderName"),
        "school_id": data.get("school"),
        "programme_id": data.get("programme"),
        "department_id": data.get("department"),
        "batch_id": data.get("batch"),
        "subject_id": data.get("subject_id"),
        "semester": data.get("semester"),
        "exam_title": data.get("examdetails"),
        "exam_type": data.get("examtype"),
        "exam_date": data.get("examdate"),
        "weightage": data.get("weightage"),
        "covered_cos": covered_cos,
        "faculty_id": faculty_id,
        "is_course_exit_summary": data.get("is_course_exit_summary") in [True, "true", "True", 1, "1"],
    }

    try:
        exam_doc = create_exam_document(exam_payload, created_by=ObjectId(faculty_id))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await db["newsavedDocs"].insert_one(exam_doc)
    folder_id = result.inserted_id

    # NOTE: this ad hoc evaluationDetails shape (folder_id/created_by/evaluation_details)
    # is a pre-existing quirk in the original Flask app — it differs from the
    # exam_id/faculty_id/totalMarks/questionEvaluationDetails shape the dedicated
    # evaluation endpoints (app/api/routers/evaluation.py) read and write. Kept as-is
    # for behavioral parity; it's effectively dead data unless something else reads it.
    question_evaluation_details = data.get("questionEvaluationDetails")
    if question_evaluation_details:
        eval_data = (
            json.loads(question_evaluation_details)
            if isinstance(question_evaluation_details, str)
            else question_evaluation_details
        )
        await db["evaluationDetails"].insert_one({
            "folder_id": folder_id,
            "created_by": ObjectId(faculty_id),
            "evaluation_details": eval_data,
            "created_at": datetime.now(timezone.utc),
        })

    return {"success": True, "message": "Folder created successfully", "folder_id": str(folder_id)}


# =====================================================
# UPLOAD QUESTION PAPER (+ background Gemini text extraction)
# =====================================================

@router.post("/upload-question-paper/{folder_id}", dependencies=[Depends(ai_rate_limit)])
async def upload_question_paper(
    folder_id: str,
    payload: UploadQuestionPaperRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, faculty_id = await _require_faculty(identity, db)

    if not ObjectId.is_valid(folder_id):
        raise HTTPException(status_code=400, detail="Invalid folder ID")

    folder_object_id = ObjectId(folder_id)
    folder = await db["newsavedDocs"].find_one({"_id": folder_object_id, "faculty_id": ObjectId(faculty_id)})
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found or unauthorized")

    questionpaper_url = payload.questionpaper_url
    file_id = payload.fileId
    filename = payload.filename
    no_of_question = payload.no_of_question

    # Text extraction is a Gemini call, so it's the only part of this
    # endpoint gated by the institute's token budget — the upload itself
    # always succeeds even if the institute is out of tokens.
    budget = await check_institute_token_budget(db, str(faculty_id), ["gemini"])

    now = datetime.now(timezone.utc)
    await db["newsavedDocs"].update_one(
        {"_id": folder_object_id},
        {"$set": {
            "question_paper": {
                "url": questionpaper_url,
                "fileId": file_id,
                "filename": filename,
                "no_of_questions": int(no_of_question),
                "text": None,
                "text_at": None,
                "text_error": None if budget["allowed"] else budget["message"],
            },
            "updated_at": now,
        }},
    )

    if budget["allowed"]:
        await enqueue(
            "run_extract_question_paper_text",
            str(folder_object_id), questionpaper_url, str(faculty_id), filename,
        )

    updated_exam = await db["newsavedDocs"].find_one({"_id": folder_object_id})

    message = "Question paper uploaded. Text extraction is running in the background."
    if not budget["allowed"]:
        message = f"Question paper uploaded, but text extraction was skipped: {budget['message']}"

    return {
        "success": True,
        "message": message,
        "token_warnings": budget["warnings"],
        "exam": {
            "id": str(updated_exam["_id"]),
            "folder_name": updated_exam.get("folder_name"),
            "question_paper": updated_exam.get("question_paper"),
            "updated_at": updated_exam.get("updated_at").isoformat() if updated_exam.get("updated_at") else None,
        },
    }


# =====================================================
# LIST / FILTER FOLDERS
# =====================================================

@router.get("/newsaved-documents")
async def get_all_folders(
    page: int = Query(1),
    limit: int = Query(10),
    school: str | None = Query(None),
    programme: str | None = Query(None),
    department: str | None = Query(None),
    batch: str | None = Query(None),
    semester: str | None = Query(None),
    subject: str | None = Query(None),
    is_archived: str | None = Query(None),
    search: str | None = Query(None),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, faculty_id = await _require_faculty(identity, db)

    if not ObjectId.is_valid(faculty_id):
        raise HTTPException(status_code=400, detail="Invalid user")

    skip = (page - 1) * limit
    match_stage: Dict[str, Any] = {"faculty_id": ObjectId(faculty_id)}

    if is_archived not in [None, "", "undefined"]:
        match_stage["is_archived"] = str(is_archived).lower() == "true"
    if semester not in [None, "", "undefined"]:
        match_stage["semester"] = int(semester)
    if school not in [None, "", "undefined"] and ObjectId.is_valid(school):
        match_stage["school_id"] = ObjectId(school)
    if programme not in [None, "", "undefined"] and ObjectId.is_valid(programme):
        match_stage["programme_id"] = ObjectId(programme)
    if department not in [None, "", "undefined"] and ObjectId.is_valid(department):
        match_stage["department_id"] = ObjectId(department)
    if batch not in [None, "", "undefined"] and ObjectId.is_valid(batch):
        match_stage["batch_id"] = ObjectId(batch)
    if subject not in [None, "", "undefined"] and ObjectId.is_valid(subject):
        match_stage["subject_id"] = ObjectId(subject)

    pipeline = [
        {"$match": match_stage},
        {"$lookup": {"from": "schoolDetails", "localField": "school_id", "foreignField": "_id", "as": "school"}},
        {"$unwind": {"path": "$school", "preserveNullAndEmptyArrays": True}},
        {"$lookup": {"from": "programmeDetails", "localField": "programme_id", "foreignField": "_id", "as": "programme"}},
        {"$unwind": {"path": "$programme", "preserveNullAndEmptyArrays": True}},
        {"$lookup": {"from": "departmentDetails", "localField": "department_id", "foreignField": "_id", "as": "department"}},
        {"$unwind": {"path": "$department", "preserveNullAndEmptyArrays": True}},
        {"$lookup": {"from": "batchDetails", "localField": "batch_id", "foreignField": "_id", "as": "batch"}},
        {"$unwind": {"path": "$batch", "preserveNullAndEmptyArrays": True}},
        {"$lookup": {"from": "subjectDetails", "localField": "subject_id", "foreignField": "_id", "as": "subject"}},
        {"$unwind": {"path": "$subject", "preserveNullAndEmptyArrays": True}},
        {"$lookup": {"from": "users", "localField": "faculty_id", "foreignField": "_id", "as": "faculty"}},
        {"$unwind": {"path": "$faculty", "preserveNullAndEmptyArrays": True}},
    ]

    search_clause = search_regex(search)
    if search_clause:
        pipeline.append({
            "$match": {
                "$or": [
                    {"folder_name": search_clause},
                    {"exam_title": search_clause},
                    {"subject.subject_name": search_clause},
                    {"subject.subject_code": search_clause},
                ]
            }
        })

    count_result = await db["newsavedDocs"].aggregate(pipeline + [{"$count": "total"}]).to_list(1)
    total_documents = count_result[0]["total"] if count_result else 0

    pipeline.extend([{"$sort": {"created_at": -1}}, {"$skip": skip}, {"$limit": limit}])

    results = [doc async for doc in db["newsavedDocs"].aggregate(pipeline)]
    response = []
    for doc in results:
        response.append({
            "id": str(doc["_id"]),
            "folder_name": doc.get("folder_name"),
            "semester": doc.get("semester"),
            "exam_title": doc.get("exam_title"),
            "exam_type": doc.get("exam_type"),
            "exam_date": doc.get("exam_date"),
            "is_archived": doc.get("is_archived"),
            "question_paper": doc.get("question_paper"),
            "weightage": doc.get("weightage"),
            "is_course_exit_summary": doc.get("is_course_exit_summary", False),
            "school_name": doc.get("school", {}).get("school_code"),
            "programme_name": doc.get("programme", {}).get("programme_code"),
            "department_name": doc.get("department", {}).get("code"),
            "batch_name": doc.get("batch", {}).get("batch_name"),
            "subject_name": doc.get("subject", {}).get("subject_name"),
            "subject_code": doc.get("subject", {}).get("subject_code"),
            "faculty_name": doc.get("faculty", {}).get("full_name"),
            "created_at": doc.get("created_at"),
        })

    return {
        "success": True,
        "documents": response,
        "totalDocuments": total_documents,
        "totalPages": math.ceil(total_documents / limit) if limit else 0,
        "currentPage": page,
        "limit": limit,
    }


# =====================================================
# COMBINED CO ANALYSIS BY SUBJECT
# =====================================================

@router.get("/combinedCO/{subject_id}")
async def get_combinedco_using_subjectid(subject_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(subject_id):
        raise HTTPException(status_code=400, detail="Invalid subject_id")

    exams = [e async for e in db["newsavedDocs"].find({"subject_id": ObjectId(subject_id)})]
    if not exams:
        raise HTTPException(status_code=404, detail="No exams found for this subject")

    final_response = []
    for exam in exams:
        exam_id = exam["_id"]
        weightage = exam.get("weightage", 0)
        answers = [a async for a in db["answerDetails"].find({"exam_id": exam_id})]
        if not answers:
            continue

        students_list = []
        for answer in answers:
            student_questions = []
            for q in answer.get("questionwise_marking", []):
                cos_list = []
                for c in q.get("cos", []):
                    obtained = c.get("final_co_marks")
                    if obtained is None:
                        obtained = c.get("ai_marks", 0)
                    cos_list.append({
                        "co_code": c.get("co_code"),
                        "obtained_marks": round(float(obtained or 0), 2),
                        "max_marks": round(float(c.get("max_marks", 0)), 2),
                    })
                student_questions.append({"question_no": q.get("question_no"), "cos": cos_list})

            students_list.append({
                "answer_id": str(answer["_id"]),
                "student_name": answer.get("student_name", ""),
                "filename": answer.get("filename"),
                "questions": student_questions,
            })

        final_response.append({
            "exam_id": str(exam_id),
            "folder_name": exam.get("folder_name"),
            "weightage": float(weightage),
            "students": students_list,
            "is_course_exit_summary": exam.get("is_course_exit_summary", False),
        })

    return {"subject_id": subject_id, "exams": final_response}


# =====================================================
# EXAMS BY SUBJECT (with evaluation progress)
# =====================================================

@router.get("/newsaved-documents-subject/{subject_id}")
async def get_exams_using_subjectid(subject_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(subject_id):
        raise HTTPException(status_code=400, detail="Invalid subject ID")

    exams = [e async for e in db["newsavedDocs"].find({"subject_id": ObjectId(subject_id)})]
    if not exams:
        raise HTTPException(status_code=404, detail="No exams found for this subject")

    response = []
    for exam in exams:
        exam_id = exam["_id"]
        total_sheets = await db["answerDetails"].count_documents({"exam_id": exam_id})
        evaluated_sheets = await db["answerDetails"].count_documents(
            {"exam_id": exam_id, "evaluated_at": {"$exists": True}}
        )
        progress = round((evaluated_sheets / total_sheets) * 100, 2) if total_sheets > 0 else 0

        response.append({
            "id": str(exam_id),
            "folder_name": exam.get("folder_name"),
            "exam_type": exam.get("exam_type"),
            "exam_date": exam.get("exam_date").isoformat() if exam.get("exam_date") else None,
            "weightage": exam.get("weightage"),
            "total_sheets": total_sheets,
            "evaluated_sheets": evaluated_sheets,
            "evaluation_progress": progress,
            "is_archived": exam.get("is_archived"),
        })

    return {"exams": response}


# =====================================================
# GET EXAM BY ID
# =====================================================

@router.get("/newsaved-documents/{folder_id}")
async def get_exam_using_examid(folder_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(folder_id):
        raise HTTPException(status_code=400, detail="Invalid exam ID")

    folder = await db["newsavedDocs"].find_one({"_id": ObjectId(folder_id)})
    if not folder:
        raise HTTPException(status_code=404, detail="Exam not found")

    return {
        "id": str(folder["_id"]),
        "folder_name": folder.get("folder_name"),
        "school_id": str(folder["school_id"]) if folder.get("school_id") else None,
        "programme_id": str(folder["programme_id"]) if folder.get("programme_id") else None,
        "department_id": str(folder["department_id"]) if folder.get("department_id") else None,
        "batch_id": str(folder["batch_id"]) if folder.get("batch_id") else None,
        "subject_id": str(folder["subject_id"]) if folder.get("subject_id") else None,
        "question_paper": folder.get("question_paper"),
        "semester": folder.get("semester"),
        "covered_cos": folder.get("covered_cos"),
        "weightage": folder.get("weightage"),
        "is_course_exit_summary": folder.get("is_course_exit_summary", False),
        "exam_title": folder.get("exam_title"),
        "exam_type": folder.get("exam_type"),
        "exam_date": folder.get("exam_date").isoformat() if folder.get("exam_date") else None,
        "is_archived": folder.get("is_archived"),
        "created_at": folder.get("created_at").isoformat() if folder.get("created_at") else None,
        "updated_at": folder.get("updated_at").isoformat() if folder.get("updated_at") else None,
    }


# =====================================================
# UPDATE EXAM
# =====================================================

@router.put("/update-exam/{folder_id}")
async def update_exam(
    folder_id: str,
    request: Request,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    data = dict(await request.form())
    user, faculty_id = await _require_faculty(identity, db)

    if not ObjectId.is_valid(folder_id):
        raise HTTPException(status_code=400, detail="Invalid folder ID")

    folder_object_id = ObjectId(folder_id)
    folder = await db["newsavedDocs"].find_one({"_id": folder_object_id})
    if not folder:
        raise HTTPException(status_code=404, detail="Exam not found")

    if folder.get("question_paper", {}).get("url"):
        raise HTTPException(status_code=400, detail="Cannot edit examination details after question paper upload")

    update_data: Dict[str, Any] = {}

    object_id_fields = {
        "school_id": "school", "programme_id": "programme", "department_id": "department",
        "batch_id": "batch", "subject_id": "subject_id",
    }
    for db_field, frontend_field in object_id_fields.items():
        if frontend_field in data and ObjectId.is_valid(data[frontend_field]):
            update_data[db_field] = ObjectId(data[frontend_field])

    if "folderName" in data:
        update_data["folder_name"] = data["folderName"]
    if "semester" in data:
        update_data["semester"] = int(data["semester"])
    if "examdetails" in data:
        update_data["exam_title"] = data["examdetails"]
    if "weightage" in data:
        update_data["weightage"] = data["weightage"]
    if "covered_cos" in data:
        update_data["covered_cos"] = data["covered_cos"]
    if "is_course_exit_summary" in data:
        update_data["is_course_exit_summary"] = data["is_course_exit_summary"] in [True, "true", "True", 1, "1"]
    if "examtype" in data:
        update_data["exam_type"] = data["examtype"]
    if data.get("examdate"):
        update_data["exam_date"] = datetime.fromisoformat(data["examdate"])

    update_data["updated_at"] = datetime.now(timezone.utc)

    if len(update_data) == 1:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    result = await db["newsavedDocs"].update_one({"_id": folder_object_id}, {"$set": update_data})
    if result.modified_count == 0:
        return {"message": "No changes made"}

    return {"success": True, "message": "Examination details updated successfully"}


# =====================================================
# RENAME / DELETE FOLDER
# =====================================================

@router.put("/rename-folder")
async def rename_folder(payload: RenameFolderRequest, db: AsyncIOMotorDatabase = Depends(get_database)):
    exam_id = payload.id
    new_foldername = payload.newFoldername

    if not ObjectId.is_valid(exam_id):
        raise HTTPException(status_code=400, detail="Invalid folder ID")

    result = await db["newsavedDocs"].update_one(
        {"_id": ObjectId(exam_id)}, {"$set": {"folder_name": new_foldername}}
    )
    if result.modified_count == 1:
        return {"success": True, "message": "Folder renamed successfully!"}

    raise HTTPException(status_code=500, detail="Folder rename failed.")


@router.delete("/delete-folder")
async def delete_folder(payload: DeleteFolderRequest, db: AsyncIOMotorDatabase = Depends(get_database)):
    exam_id = payload.id
    if not ObjectId.is_valid(exam_id):
        raise HTTPException(status_code=400, detail="Invalid folder ID")

    exam_object_id = ObjectId(exam_id)
    delete_result = await db["newsavedDocs"].delete_one({"_id": exam_object_id})
    await db["answerDetails"].delete_many({"exam_id": exam_object_id})
    await db["evaluationDetails"].delete_many({"exam_id": exam_object_id})

    if delete_result.deleted_count == 1:
        return {"success": True, "message": "Folder deleted successfully!"}

    raise HTTPException(status_code=500, detail="Folder deletion failed.")


# =====================================================
# DOWNLOAD FOLDER AS ZIP
# =====================================================

@router.get("/download-folder/{folder_id}")
async def download_folder(folder_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(folder_id):
        raise HTTPException(status_code=400, detail="Invalid folder ID")

    exam_object_id = ObjectId(folder_id)
    folder = await db["newsavedDocs"].find_one({"_id": exam_object_id})
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    answers = [a async for a in db["answerDetails"].find({"exam_id": exam_object_id})]
    if not answers:
        raise HTTPException(status_code=404, detail="No answer scripts found in this folder")

    def _fetch(url: str) -> bytes:
        try:
            return safe_get(url, timeout=60)
        except SsrfError as exc:
            logging.warning("download-folder: skipping unsafe url: %s", exc)
            return b""

    def _build_zip() -> bytes:
        # Build the (zip path, url) work list in the same order as before,
        # fetch every URL concurrently, then assemble the zip. Same entries,
        # same PDF bytes, same order — only the network fetches parallelize.
        jobs: list[tuple[str, str]] = []
        for answer in answers:
            filename = answer.get("filename", "AnswerScript.pdf")
            if not filename.lower().endswith(".pdf"):
                filename = f"{filename}.pdf"
            student_name = filename.replace(".pdf", "").replace(".PDF", "")

            if answer.get("answer_script_url"):
                jobs.append((f"{student_name}/{filename}", answer["answer_script_url"]))
            if answer.get("evaluated_report_url"):
                jobs.append((f"{student_name}/{student_name}_result.pdf", answer["evaluated_report_url"]))

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            contents = list(pool.map(lambda j: _fetch(j[1]), jobs))

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for (zip_path, _url), content in zip(jobs, contents):
                if content[:4] == b"%PDF":
                    zip_file.writestr(zip_path, content)

        zip_buffer.seek(0)
        return zip_buffer.read()

    zip_bytes = await asyncio.to_thread(_build_zip)

    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={folder.get('folder_name', 'folder')}.zip"},
    )


# =====================================================
# FOLDER DETAILS (answers + rubric)
# =====================================================

@router.get("/folder-details/{folder_id}")
async def get_folder_details(folder_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(folder_id):
        raise HTTPException(status_code=400, detail="Invalid folder ID")

    folder_object_id = ObjectId(folder_id)
    folder = await db["newsavedDocs"].find_one({"_id": folder_object_id})
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    answers = [a async for a in db["answerDetails"].find({"exam_id": folder_object_id})]

    evaluated_answersheets = []
    for answer in answers:
        answer_data = {
            "answer_id": str(answer["_id"]),
            "exam_id": str(answer.get("exam_id")),
            "faculty_id": str(answer.get("faculty_id")) if answer.get("faculty_id") else None,
            "filename": answer.get("filename", "AnswerScript.pdf"),
            "answer_script_url": answer.get("answer_script_url"),
            "fileId": answer.get("fileId"),
            "evaluated_report_url": answer.get("evaluated_report_url"),
            "evaluated_report_fileId": answer.get("evaluated_report_fileId"),
            "questionwise_marking": answer.get("questionwise_marking", []),
            "total_ai_marks": answer.get("total_ai_marks", 0),
            "total_max_marks": answer.get("total_max_marks", 0),
            "total_final_marks": answer.get("total_final_marks", 0),
            "reviewed_by_professor": answer.get("reviewed_by_professor", False),
            "student_name": answer.get("student_name"),
            "created_at": answer.get("created_at"),
            "evaluated_at": answer.get("evaluated_at"),
            "updated_at": answer.get("updated_at"),
            "html_content": answer.get("html_content"),
        }
        for date_field in ["created_at", "evaluated_at", "updated_at"]:
            if answer_data.get(date_field) and isinstance(answer_data[date_field], datetime):
                answer_data[date_field] = answer_data[date_field].isoformat()
        evaluated_answersheets.append(answer_data)

    evaluation = await db["evaluationDetails"].find_one({"exam_id": folder_object_id})
    evaluation_details = evaluation.get("questionEvaluationDetails", []) if evaluation else []

    response = {
        "folder_id": str(folder["_id"]),
        "folder_name": folder.get("folder_name", "Untitled Folder"),
        "faculty_id": str(folder.get("faculty_id")) if folder.get("faculty_id") else None,
        "question_paper": folder.get("question_paper"),
        "evaluatedanswersheets": evaluated_answersheets,
        "questionevaluationdetails": evaluation_details,
        "total_scripts": len(evaluated_answersheets),
        "evaluated_count": sum(1 for a in evaluated_answersheets if a.get("evaluated_report_url")),
        "pending_count": sum(1 for a in evaluated_answersheets if not a.get("evaluated_report_url")),
        "reviewed_count": sum(1 for a in evaluated_answersheets if a.get("reviewed_by_professor")),
        "created_at": folder.get("created_at"),
        "updated_at": folder.get("updated_at"),
    }
    for date_field in ["created_at", "updated_at"]:
        if response.get(date_field) and isinstance(response[date_field], datetime):
            response[date_field] = response[date_field].isoformat()

    return response


# =====================================================
# ARCHIVE / RESTORE
# =====================================================

@router.post("/set-archive-status/{folder_id}")
async def set_archive_status(
    folder_id: str, payload: SetArchiveStatusRequest, db: AsyncIOMotorDatabase = Depends(get_database)
):
    is_archived = payload.is_archived

    if not ObjectId.is_valid(folder_id):
        raise HTTPException(status_code=400, detail="Invalid folder ID")

    now = datetime.now(timezone.utc)
    update_data = {"is_archived": is_archived, "updated_at": now}
    update_data["archived_at" if is_archived else "restored_at"] = now

    result = await db["newsavedDocs"].update_one({"_id": ObjectId(folder_id)}, {"$set": update_data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Folder not found or unauthorized")

    return {"success": True, "message": f"Folder {'archived' if is_archived else 'restored'} successfully"}
