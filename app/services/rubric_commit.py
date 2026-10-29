"""Serialize rubric edits with the exam-level marks/evaluation barrier."""
from datetime import datetime, timezone
from fastapi import HTTPException
from pymongo.errors import PyMongoError
from bson import ObjectId


async def commit_rubric(db, exam, details, total_marks, suggestion_source_id=None):
    from app.services.paper_version import paper_snapshot_filter
    query = {"_id": exam["_id"], "faculty_id": exam["faculty_id"],
        "covered_cos": exam.get("covered_cos"), "paper_locked": {"$ne": True},
        **paper_snapshot_filter(exam["_id"], exam.get("question_paper") or {})}
    async def save(session):
        legacy = await db["answerDetails"].find_one({"exam_id": exam["_id"], "$or": [
            {"reviewed_by_professor": True}, {"evaluated_at": {"$ne": None}},
            {"total_max_marks": {"$gt": 0}}, {"active_evaluation": {"$ne": None}},
            {"questionwise_marking.0": {"$exists": True}},
        ]}, session=session)
        if legacy:
            raise HTTPException(409, "Rubric is locked because evaluation or marks entry has started")
        result = await db["newsavedDocs"].update_one(query,
            {"$inc": {"rubric_revision": 1}}, session=session)
        if result.matched_count != 1:
            raise HTTPException(409, "Rubric is locked or the exam changed. Refresh before editing.")
        now = datetime.now(timezone.utc)
        await db["evaluationDetails"].update_one(
            {"exam_id": exam["_id"], "faculty_id": ObjectId(str(exam["faculty_id"]))},
            {"$set": {"questionEvaluationDetails": details, "totalMarks": total_marks, "updated_at": now,
                       "suggestion_source_id": suggestion_source_id},
             "$setOnInsert": {"created_at": now}}, upsert=True, session=session)
    try:
        async with await db.client.start_session() as session:
            await session.with_transaction(save)
    except PyMongoError as exc:
        raise HTTPException(503, "Rubric save could not be confirmed. Refresh before retrying; MongoDB transaction support is required.") from exc
