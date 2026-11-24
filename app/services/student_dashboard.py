# ============================================================
# Student dashboard: the real numbers for the logged-in student's own dashboard.
#
# Read-only. It reads only that student's own records, never writes, and never calls an AI service.
# Anything that does not exist in the app yet (attendance, assignments, timetable, notifications) is returned
# as None or an empty list, so the screen can show "-" and never an invented value.
# ============================================================

from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import HTTPException

from app.models.transcript import TRANSCRIPT_COLLECTION
from app.utils.batch import load_by_ids

INSTITUTE_STUDENT_ROLE = 4
RELATION_COLLECTION = "StudentSubjectRelationModel"


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _score(subject: Dict[str, Any]) -> Dict[str, Any]:
    return {"subject": subject.get("subject"), "marks": _number(subject.get("marks")),
            "grade": subject.get("grade"), "credits": _number(subject.get("credits"))}


async def _transcripts(db, student: Dict[str, Any], batch_ids: set) -> List[Dict[str, Any]]:
    """The student's own transcript records. The enrollment number is unique, so it always counts; the roll number can
    repeat between batches, so it counts only for a record of a batch the student is enrolled in."""
    enrollment, roll = _text(student.get("enrollment_no")), _text(student.get("roll_no"))
    keys = [key for key in {enrollment, roll} if key]
    if not keys:
        return []
    projection = {"_id": 0, "student_id": 1, "batch_id": 1, "semester_no": 1, "term_label": 1,
                  "subjects": 1, "tgpa": 1, "cgpa": 1, "is_stale": 1}
    found = [doc async for doc in db[TRANSCRIPT_COLLECTION].find(
        {"institute_id": student["institute_id"], "student_id": {"$in": keys}}, projection).sort("semester_no", 1)]
    return [doc for doc in found
            if _text(doc.get("student_id")) == enrollment and enrollment
            or (_text(doc.get("student_id")) == roll and doc.get("batch_id") in batch_ids)]


async def build_dashboard(db, user_id: str) -> Dict[str, Any]:
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=404, detail="Student not found")
    student = await db["studentDetails"].find_one(
        {"user_id": ObjectId(user_id), "role": INSTITUTE_STUDENT_ROLE, "is_deleted": {"$ne": True}})
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    relations = [r async for r in db[RELATION_COLLECTION].find(
        {"student_id": student["_id"], "user_id": ObjectId(user_id), "is_deleted": {"$ne": True}},
        {"subject_id": 1, "semester": 1, "batch_id": 1})]
    subjects = await load_by_ids(db, "subjectDetails", (r.get("subject_id") for r in relations),
                                 {"_id": 1}, match={"is_deleted": {"$ne": True}})
    batch_ids = {r.get("batch_id") for r in relations if r.get("batch_id")}
    batches = await load_by_ids(db, "batchDetails", batch_ids, {"total_semesters": 1},
                                match={"institute_id": student["institute_id"]})
    totals = [int(b["total_semesters"]) for b in batches.values()
              if isinstance(b.get("total_semesters"), (int, float)) and not isinstance(b.get("total_semesters"), bool)]

    records = await _transcripts(db, student, batch_ids)
    usable = [r for r in records if not r.get("is_stale")]
    latest = max(usable, key=lambda r: r.get("semester_no") or 0) if usable else None

    semesters = sorted({r.get("semester") for r in relations if isinstance(r.get("semester"), int)}
                       | {r.get("semester_no") for r in records if isinstance(r.get("semester_no"), int)})

    return {
        "name": student.get("name"),
        "subjects_count": len(subjects),
        "semesters": semesters,
        "total_semesters": max(totals) if totals else None,
        "cgpa": _number(latest.get("cgpa")) if latest else None,
        "latest_semester": latest.get("semester_no") if latest else None,
        "scores": [_score(s) for s in (latest.get("subjects") or [])] if latest else [],
        "results_stale": any(r.get("is_stale") for r in records),
        # Not stored anywhere in the app yet: the screen shows "-" for these.
        "attendance": None,
        "assignments": None,
        "timetable": [],
        "notifications": [],
    }
