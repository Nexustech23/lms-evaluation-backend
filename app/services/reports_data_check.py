# ============================================================
# Data Check report: which courses, exams and results are incomplete, so an admin knows what to fix before the
# numbers in the other reports can be trusted. Read-only, college institutes only, no AI.
#
# It uses the same scope as the Reports overview, and the same rules, so a count on the overview tile always equals the
# count in this report. Students appear only as counts (how many results need regenerating per batch and semester).
# ============================================================

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId

from app.services.evaluation_review import pending_review
from app.services.reports_overview import load_scope, scope_filters, scope_summary

LIST_LIMIT = 200                        # rows returned per list; the count always covers every row


def _section(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"items": items[:LIST_LIMIT], "total": len(items), "truncated": len(items) > LIST_LIMIT}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


async def build_data_check(db, institute_id: ObjectId, programme: Optional[str] = None, batch: Optional[str] = None,
                           semester: Optional[int] = None) -> Dict[str, Any]:
    ctx = await load_scope(db, institute_id, programme, batch, semester)
    batch_by_id, programme_name = ctx["batch_by_id"], ctx["programme_name"]
    subjects, exams, answers = ctx["subjects"], ctx["exams"], ctx["answers"]
    subject_by_id = {s["_id"]: s for s in subjects}

    # -- courses with no CO-PO mapping
    courses = sorted(({
        "id": str(s["_id"]), "name": s.get("subject_name"), "code": s.get("subject_code"),
        "programme": programme_name.get(s.get("programme_id")), "semester": s.get("semester"),
        "batch": (batch_by_id.get(s.get("batch_id")) or {}).get("batch_name"),
    } for s in subjects if not s.get("co_po_matrix")),
        key=lambda r: (_text(r["programme"]), r["semester"] or 0, _text(r["name"])))

    # -- exams with no covered COs (the faculty name comes from the users collection, which exams point to)
    untagged = [e for e in exams if not e.get("covered_cos")]
    faculty_ids = [e["faculty_id"] for e in untagged if e.get("faculty_id") is not None]
    faculty_name = ({u["_id"]: u.get("fullName") async for u in db["users"].find({"_id": {"$in": faculty_ids}})}
                    if faculty_ids else {})
    exam_rows = sorted(({
        "id": str(e["_id"]), "name": e.get("exam_title") or e.get("folder_name"), "type": e.get("exam_type"),
        "subject": (subject_by_id.get(e.get("subject_id")) or {}).get("subject_name"), "semester": e.get("semester"),
        "faculty": faculty_name.get(e.get("faculty_id")),
    } for e in untagged), key=lambda r: (r["semester"] or 0, _text(r["subject"]), _text(r["name"])))

    # -- answer sheets still waiting for review, per exam
    exam_by_id = {e["_id"]: e for e in exams}
    per_exam: Dict[Any, Dict[str, int]] = {}
    for answer in answers:
        entry = per_exam.setdefault(answer.get("exam_id"), {"total": 0, "pending": 0})
        entry["total"] += 1
        entry["pending"] += 1 if pending_review(answer) else 0
    pending_total = sum(v["pending"] for v in per_exam.values())
    evaluation_rows = sorted(({
        "id": str(exam_id), "name": exam_by_id[exam_id].get("exam_title") or exam_by_id[exam_id].get("folder_name"),
        "subject": (subject_by_id.get(exam_by_id[exam_id].get("subject_id")) or {}).get("subject_name"),
        "semester": exam_by_id[exam_id].get("semester"), "total": v["total"], "pending": v["pending"],
    } for exam_id, v in per_exam.items() if v["pending"] and exam_id in exam_by_id),
        key=lambda r: (-r["pending"], _text(r["name"])))

    # -- results that need regenerating, per batch and semester (counts only, no student names or ids)
    stale_students: Dict[Any, set] = {}
    for record in ctx["every_record"]:
        if record.get("is_stale"):
            stale_students.setdefault((record.get("batch_id"), record.get("semester_no")), set()).add(
                record.get("student_id"))
    stale_rows = sorted(({
        "batch": (batch_by_id.get(key[0]) or {}).get("batch_name"), "semester": key[1], "students": len(ids),
    } for key, ids in stale_students.items()), key=lambda r: (_text(r["batch"]), r["semester"] or 0))
    stale_total = ctx["stale_results"]

    problems = len(courses) + len(exam_rows) + stale_total
    checks_ran = bool(subjects or exams)
    return {
        "scope": scope_summary(ctx),
        "filters": scope_filters(ctx),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "checks_ran": checks_ran,
        "all_clear": checks_ran and problems == 0 and pending_total == 0,
        "summary": {
            "courses": {"count": len(courses), "of": len(subjects)},
            "exams": {"count": len(exam_rows), "of": len(exams)},
            "evaluation": {"pending": pending_total, "of": len(answers)},
            "stale": {"count": stale_total},
            "problems": problems if checks_ran else None,           # the same number as the overview's Data checks tile
        },
        "courses": _section(courses),
        "exams": _section(exam_rows),
        "evaluation": _section(evaluation_rows),
        "stale": _section(stale_rows),
    }
