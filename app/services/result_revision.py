"""Revision fences shared by result mutations and transcript publication."""
from datetime import datetime, timezone
from fastapi import HTTPException
from pymongo.errors import PyMongoError


def source_snapshot(batch):
    return (batch.get("result_source_revision", 0), batch.get("transcript_write_revision", 0))


async def transaction(db, operation):
    try:
        async with await db.client.start_session() as session:
            return await session.with_transaction(operation)
    except PyMongoError as exc:
        raise HTTPException(503, "Result change could not be confirmed. Refresh and retry; MongoDB transactions are required.") from exc


async def touch_result_source(db, exam, session):
    subject = await db["subjectDetails"].find_one({"_id": exam.get("subject_id")}, session=session)
    if not subject:
        raise HTTPException(409, "Exam subject link needs correction before changing results")
    if not subject.get("batch_id"):
        return  # Non-batch subjects do not publish academic transcripts.
    batch_id, institute_id, semester = subject["batch_id"], subject.get("institute_id"), subject.get("semester")
    if institute_id is None or semester is None:
        raise HTTPException(409, "Subject batch/institute/semester needs correction")
    result = await db["batchDetails"].update_one({"_id": batch_id, "institute_id": institute_id},
        {"$inc": {"result_source_revision": 1}}, session=session)
    if result.matched_count != 1:
        raise HTTPException(409, "Subject batch link needs correction")
    scope = {"institute_id": institute_id, "batch_id": batch_id}
    stale = {"is_stale": True, "stale_at": datetime.now(timezone.utc),
             "stale_reason": "Source results changed. Regenerate the affected semester."}
    await db["academic_transcripts"].update_many({**scope, "semester_no": int(semester)},
        {"$set": {**stale, "results_stale": True}}, session=session)
    await db["academic_transcripts"].update_many({**scope, "semester_no": {"$gt": int(semester)}},
        {"$set": {**stale, "cgpa_stale": True}}, session=session)


async def update_answer_result(db, exam, query, changes, paper_guard=None, review_event=None):
    async def update(session):
        parent = {"_id": exam["_id"], "faculty_id": exam["faculty_id"]}
        if paper_guard:
            parent.update(paper_guard)
        guard = await db["newsavedDocs"].update_one(parent,
            {"$inc": {"answer_write_revision": 1}}, session=session)
        if guard.matched_count != 1:
            raise HTTPException(409, "Exam or paper changed while saving results")
        current = await db["newsavedDocs"].find_one({"_id": exam["_id"]}, session=session)
        await touch_result_source(db, current, session)
        result = await db["answerDetails"].update_one(query, changes, session=session)
        if result.matched_count != 1:
            raise HTTPException(409, "Answer changed or was deleted while saving results")
        if review_event is not None:
            await db["answerReviewHistory"].insert_one(review_event, session=session)
        return result
    return await transaction(db, update)
