# ============================================================
# Student results: the logged-in student's own semester results, for the student's Result page.
#
# Read-only. It reads only that student's own transcript records (the same safe matching as the dashboard), never
# writes, and never calls an AI service. Records that need regenerating are not shown; the reply says so.
# ============================================================

from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import HTTPException

from app.services.student_dashboard import (
    INSTITUTE_STUDENT_ROLE, RELATION_COLLECTION, _number, _transcripts,
)


def _subject(row: Dict[str, Any]) -> Dict[str, Any]:
    return {"subject": row.get("subject"), "credits": _number(row.get("credits")), "marks": _number(row.get("marks")),
            "grade": row.get("grade"), "grade_point": _number(row.get("gradePoint")),
            "credit_points": _number(row.get("creditPoints"))}


def _semester(record: Dict[str, Any]) -> Dict[str, Any]:
    return {"semester": record.get("semester_no"), "label": record.get("term_label"),
            "subjects": [_subject(row) for row in (record.get("subjects") or []) if isinstance(row, dict)],
            "overall_total": _number(record.get("overall_total")), "total_credits": _number(record.get("total_credits")),
            "total_credit_points": _number(record.get("total_credit_points")),
            "tgpa": _number(record.get("tgpa")), "cgpa": _number(record.get("cgpa"))}


async def build_results(db, user_id: str) -> Dict[str, Any]:
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=404, detail="Student not found")
    student = await db["studentDetails"].find_one(
        {"user_id": ObjectId(user_id), "role": INSTITUTE_STUDENT_ROLE, "is_deleted": {"$ne": True}})
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    relations = [r async for r in db[RELATION_COLLECTION].find(
        {"student_id": student["_id"], "user_id": ObjectId(user_id), "is_deleted": {"$ne": True}}, {"batch_id": 1})]
    batch_ids = {r.get("batch_id") for r in relations if r.get("batch_id")}

    records = await _transcripts(db, student, batch_ids)
    usable = sorted((r for r in records if not r.get("is_stale")), key=lambda r: r.get("semester_no") or 0)
    latest: Optional[Dict[str, Any]] = usable[-1] if usable else None
    semesters: List[Dict[str, Any]] = [_semester(r) for r in usable]

    return {
        "name": student.get("name"),
        "enrollment_no": student.get("enrollment_no"),
        "programme_name": student.get("programme_name"),
        "semesters": semesters,
        "cgpa": _number(latest.get("cgpa")) if latest else None,
        "results_stale": any(r.get("is_stale") for r in records),
    }
