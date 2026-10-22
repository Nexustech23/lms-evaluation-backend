"""Publish faculty marks, a matching report and an immutable review event."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from bson import ObjectId
from fastapi import HTTPException

from app.services.evaluation_review import OPTIONAL_REVIEW_POLICY, review_count
from app.services.grading import generate_evaluation_report_html
from app.services.imagekit import upload_file_to_imagekit, delete_imagekit_file
from app.services.pdf_render import render_html_to_pdf
from app.services.result_revision import update_answer_result
from app.utils.transcript_generation_helper import refresh_transcript_for_exam


def co_projection(questions):
    return [{"question_no": q["question_no"], "cos": [
        {"co_code": c["co_code"], "ai_marks": c["ai_marks"],
         "max_marks": c["max_marks"], "grace_marks": c.get("grace_marks", 0),
         "final_co_marks": c.get("final_co_marks", c["ai_marks"])}
        for c in q.get("cos", [])]} for q in questions if q.get("cos")]


async def publish_faculty_review(db, exam, answer, questions, identity, revision=None):
    current_revision = answer.get("result_revision", 0)
    if revision is not None and revision != current_revision:
        raise HTTPException(409, "Marks changed since this review was opened. Refresh and review the latest result.")
    now = datetime.now(timezone.utc)
    total = round(sum(q["final_marks"] for q in questions), 2)
    maximum = answer.get("total_max_marks") or sum(q["max_marks"] for q in questions)
    html = generate_evaluation_report_html({"questionwise_marking": questions,
        "review_policy": OPTIONAL_REVIEW_POLICY, "faculty_reviewed": True,
        "summary": {"total_max_marks": maximum}}, answer.get("student_name") or "Student", maximum)
    pdf = await asyncio.to_thread(render_html_to_pdf, html)
    upload = await asyncio.to_thread(upload_file_to_imagekit, pdf,
        f"faculty_review_{answer['_id']}_{uuid4().hex}.pdf", "/evaluated-reports",
        ["evaluated", "faculty-reviewed", str(answer["_id"])])
    version_filter = {"result_revision": current_revision} if "result_revision" in answer else {
        "result_revision": {"$exists": False}}
    event = {"_id": ObjectId(), "answer_id": answer["_id"], "exam_id": exam["_id"],
        "reviewed_by": identity["user_id"], "reviewed_at": now,
        "from_revision": current_revision, "to_revision": current_revision + 1,
        "previous_questionwise_marking": deepcopy(answer.get("questionwise_marking", [])),
        "questionwise_marking": deepcopy(questions),
        "previous_total_final_marks": answer.get("total_final_marks"),
        "total_final_marks": total, "total_ai_marks": answer.get("total_ai_marks"),
        "previous_report_url": answer.get("evaluated_report_url"),
        "previous_report_file_id": answer.get("evaluated_report_fileId"),
        "report_url": upload["url"], "report_file_id": upload["file_id"]}
    try:
        await update_answer_result(db, exam,
            {"_id": answer["_id"], "exam_id": exam["_id"], **version_filter},
            {"$set": {"questionwise_marking": questions, "total_final_marks": total,
                "reviewed_by_professor": True, "evaluation_status": "faculty_reviewed",
                "review_policy": OPTIONAL_REVIEW_POLICY, "review_pending_count": review_count(questions),
                "co": co_projection(questions), "report_is_draft": False,
                "evaluated_report_url": upload["url"], "evaluated_report_fileId": upload["file_id"],
                "reviewed_at": now, "reviewed_by": identity["user_id"], "updated_at": now},
             "$inc": {"result_revision": 1},
             "$addToSet": {"verified_storage_ids": upload["file_id"]}}, review_event=event)
    except Exception:
        # An unknown commit outcome may have saved the report. Only remove a
        # fresh upload if a database read confirms it was not committed.
        try:
            current = await db["answerDetails"].find_one({"_id": answer["_id"]})
            if current is not None and upload["file_id"] not in current.get("verified_storage_ids", []):
                await asyncio.to_thread(delete_imagekit_file, upload["file_id"])
        except Exception:
            pass
        raise
    # Source revision invalidation is in the transaction. Existing transcripts
    # are refreshed before returning; failed refreshes remain explicitly stale.
    refresh = await refresh_transcript_for_exam(db, exam["_id"])
    return {"success": True, "total_final_marks": total, "result_revision": current_revision + 1,
        "evaluated_report_url": upload["url"], "evaluation_status": "faculty_reviewed",
        "transcript_refresh": refresh or "not_applicable"}
