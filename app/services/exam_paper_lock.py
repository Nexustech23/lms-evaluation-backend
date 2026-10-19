"""A permanent exam-level barrier once evaluation or marks entry starts."""
from fastapi import HTTPException
from app.services.paper_version import paper_snapshot_filter


async def lock_exam_for_marks(db, exam_id, paper=None):
    query = paper_snapshot_filter(exam_id, paper) if paper is not None else {"_id": exam_id}
    result = await db["newsavedDocs"].update_one(
        query, {"$set": {"paper_locked": True}},
    )
    if result.matched_count != 1:
        raise HTTPException(409, "Exam or paper changed. Refresh before entering or evaluating marks.")


async def require_replaceable_paper(db, exam):
    # Old records predate paper_locked. Treat pending jobs and historical marks
    # as protected too, without clearing or silently invalidating those results.
    legacy_marks = await db["answerDetails"].find_one({"exam_id": exam["_id"], "$or": [
        {"reviewed_by_professor": True}, {"evaluated_at": {"$ne": None}},
        {"total_max_marks": {"$gt": 0}}, {"active_evaluation": {"$ne": None}},
        {"questionwise_marking.0": {"$exists": True}},
    ]})
    if exam.get("paper_locked") or legacy_marks:
        raise HTTPException(409, "Question paper cannot be replaced after evaluation or marks entry has started. Create a new exam for a different paper.")
