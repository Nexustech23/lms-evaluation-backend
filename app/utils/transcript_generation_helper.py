# ============================================================
# Ported from utils/transcript_generation_helper.py. Builds
# academic_transcripts-shaped documents for one batch/semester directly
# from real exam data instead of an Excel workbook. Does not insert
# anything — callers decide whether to persist (confirm) or just inspect
# (preview).
#
# Grade-point source: exclusively app/utils/grade_points.py, the same
# table already used by relative grading and subject results — the
# Flask original's separate, numerically-different utils/transcript_
# helper.py table was confirmed dead code (zero call sites anywhere in
# the Flask repo) and is deliberately NOT ported, so there is exactly one
# grade-point source of truth across the backend.
# ============================================================

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.models.transcript import get_semesters
from app.utils.grade_points import get_grade_point

logger = logging.getLogger(__name__)


def _natural_key(value: Any) -> List[Any]:
    """Sort key so "Student 2" comes before "Student 10"."""
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(value))]


def discover_batch_roster(subject_results: Dict[str, Any]) -> List[str]:
    """Union of every student_id found across a batch/semester's per-subject results."""
    roster = set()
    for result in subject_results.values():
        roster.update(s["student_id"] for s in result["students"])
    return sorted(roster, key=_natural_key)


async def generate_transcript_for_semester(
    db: AsyncIOMotorDatabase, institute_id: ObjectId, batch_id: str, semester: str
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    # Imported here (not at module scope) to avoid a routers<->utils import
    # cycle: subject_results.py's router module lives above this util.
    from app.api.routers.subject_results import _get_subject_results

    subjects = [
        s async for s in db["subjectDetails"].find({
            "batch_id": ObjectId(batch_id), "semester": int(semester),
            "institute_id": institute_id, "is_deleted": False,
        })
    ]

    subject_results: Dict[str, Any] = {}
    for subject in subjects:
        result, code = await _get_subject_results(db, str(subject["_id"]), institute_id)
        if code == 200:
            subject_results[str(subject["_id"])] = result

    roster = discover_batch_roster(subject_results)

    documents = []
    generated_at = datetime.now(timezone.utc)
    partial_students = []

    for student_id in roster:
        subject_rows = []
        semester_credits = 0.0
        semester_credit_points = 0.0
        semester_marks = 0.0

        for subject in subjects:
            subject_id = str(subject["_id"])
            result = subject_results.get(subject_id)
            student = None
            if result:
                student = next((s for s in result["students"] if s["student_id"] == student_id), None)

            # A student with no answer sheet at all for this subject is
            # treated the same as one with no course_grade: synthetic
            # 0 marks / U grade, so total_credits stays comparable across
            # every student in the roster.
            marks = (student or {}).get("composite_percentage")
            grade = (student or {}).get("course_grade") or "U"
            if marks is None:
                marks = 0
                partial_students.append((student_id, subject.get("subject_name")))

            credits = subject.get("credits", 0) or 0
            grade_point = get_grade_point(grade)
            credit_points = round(credits * grade_point, 2)

            subject_rows.append({
                "subject": subject.get("subject_name"),
                "credits": credits,
                "marks": marks,
                "grade": grade,
                "gradePoint": grade_point,
                "creditPoints": credit_points,
                "coBreakdown": (student or {}).get("co_summary", []),
            })
            semester_credits += credits
            semester_credit_points += credit_points
            semester_marks += marks

        semester_credits = round(semester_credits, 2)
        semester_credit_points = round(semester_credit_points, 2)
        tgpa = round(semester_credit_points / semester_credits, 2) if semester_credits else 0

        prior = await get_semesters(db, student_id, institute_id, batch_id)
        prior = [p for p in prior if p.get("semester_no") != int(semester)]
        cumulative_credits = sum(p.get("total_credits", 0) for p in prior) + semester_credits
        cumulative_credit_points = sum(p.get("total_credit_points", 0) for p in prior) + semester_credit_points
        cgpa = round(cumulative_credit_points / cumulative_credits, 2) if cumulative_credits else 0

        documents.append({
            "institute_id": institute_id,
            "batch_id": ObjectId(batch_id),
            "student_id": student_id,
            "semester_no": int(semester),
            "term_label": f"Semester {semester}",
            "subjects": subject_rows,
            "overall_total": round(semester_marks, 2),
            "total_credits": semester_credits,
            "total_credit_points": semester_credit_points,
            "tgpa": tgpa,
            "cgpa": cgpa,
            "source": "generated",
            "imported_at": generated_at,
        })

    summary = {
        "student_count": len(roster),
        "subject_count": len(subjects),
        "subjects": [{"subject": s.get("subject_name"), "credits": s.get("credits", 0)} for s in subjects],
        "partial_students": [{"student_id": sid, "subject": subject_name} for sid, subject_name in partial_students],
    }

    return documents, summary


async def refresh_transcript_for_exam(db: AsyncIOMotorDatabase, exam_id: Any) -> None:
    """Fire-and-forget: if a transcript has already been generated for the
    batch/semester an exam's subject belongs to, silently regenerate it so
    newly saved/updated marks show up without an admin re-running "Generate"
    by hand. Never raises — a marks save/upload must never fail because this
    background refresh hit a problem.

    Deliberately does NOT create a transcript for a batch/semester nobody has
    generated before — only refreshes what already exists, so a single
    answer save doesn't trigger full-batch recomputation for every batch in
    the institute."""
    try:
        exam_object_id = exam_id if isinstance(exam_id, ObjectId) else ObjectId(str(exam_id))
        exam = await db["newsavedDocs"].find_one({"_id": exam_object_id}, {"subject_id": 1})
        if not exam or not exam.get("subject_id"):
            return

        subject = await db["subjectDetails"].find_one(
            {"_id": exam["subject_id"]}, {"batch_id": 1, "semester": 1, "institute_id": 1},
        )
        if not subject or not subject.get("batch_id") or subject.get("semester") is None:
            return

        institute_id = subject.get("institute_id")
        batch_id = subject["batch_id"]
        semester = subject["semester"]

        existing = await db["academic_transcripts"].find_one({
            "institute_id": institute_id, "batch_id": batch_id, "semester_no": int(semester),
        })
        if not existing:
            return  # this batch/semester's transcript was never generated — nothing to refresh

        from app.models.relative_grading import build_grading_config
        grading = await db["relativeGradings"].find_one({"university_id": institute_id})
        if not grading:
            return
        grading_config = build_grading_config(grading)
        if not grading_config:
            return

        documents, _summary = await generate_transcript_for_semester(
            db, institute_id, str(batch_id), str(semester)
        )
        if not documents:
            return

        generation_id = str(uuid.uuid4())
        for document in documents:
            document["import_id"] = generation_id

        await db["academic_transcripts"].insert_many(documents, ordered=True)
        await db["transcriptImports"].insert_one({
            "import_id": generation_id,
            "institute_id": institute_id,
            "batch_id": batch_id,
            "source": "auto-refresh",
            "record_count": len(documents),
            "imported_at": datetime.now(timezone.utc),
        })
        # Scoped to this semester only, same as the manual generate/confirm
        # flow — replaces only the prior generation of the SAME semester.
        await db["academic_transcripts"].delete_many({
            "institute_id": institute_id, "batch_id": batch_id,
            "semester_no": int(semester), "import_id": {"$ne": generation_id},
        })
    except Exception:
        logger.exception("Automatic transcript refresh failed for exam_id=%s", exam_id)
