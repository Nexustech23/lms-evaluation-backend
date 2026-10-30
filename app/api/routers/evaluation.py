# ============================================================
# EVALUATION RUBRIC ROUTER
# Ported from routes/institute/evaluation_routes.py +
# controllers/institute/evaluation_controller.py
# ============================================================

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from uuid import uuid4
from datetime import datetime, timezone
from pymongo.errors import DuplicateKeyError
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity
from app.api.answer_access import require_exam_access
from app.db.mongodb import get_database
from app.models.evaluation import validate_evaluation_details, _validate_total_marks
from app.models.exam_validation import covered_co_list
from app.services.rubric_commit import commit_rubric
from app.schemas.evaluation import SaveEvaluationDetailsRequest

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["evaluation"])


class SuggestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    retry_failed: bool = False
    regenerate: bool = False


class QuestionBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start: StrictInt = Field(ge=0)
    question_label: str = Field(min_length=1, max_length=120)


class QuestionMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    questions: list[QuestionBoundary] = Field(min_length=1, max_length=60)


@router.post("/evaluation-details/{folder_id}/question-mapping")
async def confirm_question_mapping(folder_id: str, payload: QuestionMappingRequest,
                                   identity: dict = Depends(get_current_identity),
                                   db: AsyncIOMotorDatabase = Depends(get_database)):
    from app.services.exam_paper_lock import require_replaceable_paper
    from app.services.question_mapping import source_key, mapped_blocks
    exam = await require_exam_access(identity, db, folder_id)
    await require_replaceable_paper(db, exam)
    paper = exam.get("question_paper") or {}
    if source_key(paper) != payload.source_id:
        raise HTTPException(409, "The paper changed. Reload its question mapping before confirming.")
    text, count = paper.get("text"), paper.get("no_of_questions")
    if not isinstance(text, str) or type(count) is not int or not 1 <= count <= 60:
        raise HTTPException(422, "Load a paper with a valid question count before confirming its mapping.")
    selections = [row.model_dump() for row in payload.questions]
    mapped_blocks(text, selections, count)
    result = await db["newsavedDocs"].update_one({
        "_id": exam["_id"], "faculty_id": exam["faculty_id"], "paper_locked": {"$ne": True},
        "question_paper": paper,
    }, {"$set": {"question_paper.question_mapping": {
        "source_id": payload.source_id, "questions": selections}}})
    if result.matched_count != 1:
        raise HTTPException(409, "The exam changed or was locked. Reload before confirming its mapping.")
    return {"success": True}


@router.get("/evaluation-details/{folder_id}/suggestions")
async def get_rubric_suggestions(folder_id: str, identity: dict = Depends(get_current_identity),
                                 db: AsyncIOMotorDatabase = Depends(get_database)):
    from app.services.rubric_suggestions import COLLECTION, build_context, public_job
    exam = await require_exam_access(identity, db, folder_id)
    job_id, _ = await build_context(db, exam, identity)
    row = await db[COLLECTION].find_one({"_id": job_id, "owner_user_id": str(identity["user_id"])})
    return {**public_job(row), "source_id": job_id}


@router.post("/evaluation-details/{folder_id}/suggestions")
async def suggest_rubric(folder_id: str, payload: SuggestionRequest, background_tasks: BackgroundTasks,
                         identity: dict = Depends(get_current_identity),
                         db: AsyncIOMotorDatabase = Depends(get_database)):
    from app.core.config import settings
    from app.core.queue import enqueue
    from app.core.rate_limit import ai_rate_limit
    from app.services.exam_paper_lock import require_replaceable_paper
    from app.services.rubric_suggestions import COLLECTION, build_context, public_job
    from app.utils.token_usage import check_institute_token_budget
    exam = await require_exam_access(identity, db, folder_id)
    await require_replaceable_paper(db, exam)
    job_id, context = await build_context(db, exam, identity)
    query = {"_id": job_id, "owner_user_id": str(identity["user_id"])}
    existing = await db[COLLECTION].find_one(query)
    if payload.regenerate and not payload.retry_failed:
        raise HTTPException(422, "Regeneration requires an explicit failed-attempt retry.")
    if existing and (existing["status"] != "failed" or not payload.retry_failed):
        return {**public_job(existing), "source_id": job_id, "reused": True}
    reuse_response = bool(existing and existing.get("raw_response") is not None and not payload.regenerate)
    if not reuse_response:
        if not settings.ANTHROPIC_API_KEY:
            raise HTTPException(503, "AI parameter suggestions are not configured.")
        await ai_rate_limit(identity)
        budget = await check_institute_token_budget(db, str(exam["faculty_id"]), ["claude"])
        if not budget["allowed"]:
            raise HTTPException(402, budget["message"])
    attempt_id = uuid4().hex
    pending = {"status": "queued", "attempt_id": attempt_id,
               "exam_id": str(exam["_id"]), "faculty_id": str(exam["faculty_id"]),
               "paper_version": context["paper_version"],
               "model": context["model"], "prompt_version": context["prompt_version"],
               "owner_user_id": str(identity["user_id"]), "created_at": datetime.now(timezone.utc)}
    if existing:
        clear = {"error": "", "result": "", "failure_type": ""}
        if not reuse_response:
            clear.update(raw_response="", response_saved_at="")
        result = await db[COLLECTION].update_one({**query, "status": "failed", "attempt_id": existing["attempt_id"]},
            {"$set": pending, "$unset": clear})
        claimed = result.modified_count == 1
    else:
        try:
            result = await db[COLLECTION].update_one({"_id": job_id}, {"$setOnInsert": pending}, upsert=True)
            claimed = result.upserted_id is not None
        except DuplicateKeyError:
            claimed = False
    if claimed:
        try:
            await enqueue("run_rubric_suggestion", job_id, attempt_id, context, background_tasks=background_tasks)
        except Exception:
            # Ambiguous dispatch is never automatically resubmitted: a worker may
            # have accepted the job even if its acknowledgement was lost.
            await db[COLLECTION].update_one({**query, "attempt_id": attempt_id}, {"$set": {
                "error": "Queue dispatch is unconfirmed. Reconnect to this draft; do not start another paid request."}})
    row = await db[COLLECTION].find_one(query)
    return {**public_job(row), "source_id": job_id, "reused": not claimed}


@router.get("/evaluation-details/{folder_id}")
async def get_evaluation_details(
    folder_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    exam = await require_exam_access(identity, db, folder_id, allow_institute_read=True)
    evaluation = await db["evaluationDetails"].find_one({"exam_id": exam["_id"]})
    if not evaluation:
        return {"success": True, "message": "No evaluation details found", "evaluation": None}

    evaluation["_id"] = str(evaluation["_id"])
    evaluation["faculty_id"] = str(evaluation["faculty_id"])
    evaluation["exam_id"] = str(evaluation["exam_id"])

    return {"success": True, "evaluation": evaluation}


@router.post("/evaluation-details/{folder_id}")
async def save_evaluation_details(
    folder_id: str,
    payload: SaveEvaluationDetailsRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    exam = await require_exam_access(identity, db, folder_id)
    total_marks = payload.totalMarks

    try:
        validated_details = validate_evaluation_details(payload.questionEvaluationDetails)
        total_marks = _validate_total_marks(total_marks, validated_details)
        covered_cos = set(covered_co_list(exam.get("covered_cos")))
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e) or "questionEvaluationDetails is invalid")

    used_cos = {co["co_code"] for q in validated_details for co in q["cos"]}
    unknown_cos = sorted(used_cos - covered_cos)
    if unknown_cos:
        raise HTTPException(
            status_code=400,
            detail=(
                f"These COs are used in the rubric but not declared in the exam's covered COs: "
                f"{', '.join(unknown_cos)}. Edit the exam's covered COs first, or remove them from the rubric."
            ),
        )

    if payload.suggestion_source_id:
        from app.services.rubric_suggestions import build_context
        current_source, context = await build_context(db, exam, identity)
        if current_source != payload.suggestion_source_id:
            raise HTTPException(409, "The question paper or course outcomes changed. Review suggestions for the current paper before saving.")
        if len(validated_details) != len(context["questions"]):
            raise HTTPException(422, "Evaluation rows must match the confirmed question mapping.")
        for row, block in zip(validated_details, context["questions"]):
            for key in ("question_id", "question_label", "section"):
                if row.get(key) is not None and row[key] != block[key]:
                    raise HTTPException(409, "Question mapping changed. Reload the current suggestions.")
                row[key] = block[key]
            row["question_text"] = block["text"]
    await commit_rubric(db, exam, validated_details, total_marks,
                       **({"suggestion_source_id": payload.suggestion_source_id} if payload.suggestion_source_id else {}))

    return {
        "success": True,
        "message": "Evaluation details saved successfully",
        "totalMarks": total_marks,
        "questionEvaluationDetails": validated_details,
    }
