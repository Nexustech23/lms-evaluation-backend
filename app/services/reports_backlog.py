# ============================================================
# Backlog and At-risk report: the students who need follow-up, with the reason for each, and the students who have not
# cleared a mandatory exam. College institutes only, no AI.
#
# THIS REPORT NAMES STUDENTS. So every view and every export is written to an access log (who, when, which scope, how
# many rows), and a log that cannot be written stops the request: no names are shown without a record. The rules are the
# same as the Reports overview's, so the counts equal the dashboard tiles. A student is "at risk" by a visible rule
# (failed subjects or a low CGPA), never by a hidden model, and the reasons are returned with every row.
# ============================================================

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from bson import ObjectId
from fastapi import HTTPException

from app.services.answer_identity import answer_student_id
from app.services.attendance import attendance_status, is_not_cleared
from app.services.evaluation_review import pending_review
from app.services.reports_overview import (
    AT_RISK_MIN_CGPA, AT_RISK_MIN_FAILED_PAPERS, FAIL_GRADES, PASS_TARGET_PERCENT, RELATION_COLLECTION, _failed, _grades,
    _number, backlog_label_of, load_scope, scope_filters, scope_summary,
)
from app.services.reports_result_analysis import _text_cell, _write_table, scope_words

ACCESS_LOG = "reportAccessLog"
SEGMENTS = ("all", "backlog", "at-risk")
LIST_LIMIT = 200                        # rows shown on screen; the counts cover every row
EXPORT_LIMIT = 5000                     # rows in one Excel file
SUBJECTS_SHOWN = 8                      # failed subjects listed per student before "and more"


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


async def record_access(db, institute_id: ObjectId, user_id: str, action: str, report: str, scope: Dict[str, Any],
                        segment: str, rows: int) -> None:
    """Write who looked at (or exported) a list of named students. If this fails the caller must not show the names."""
    try:
        await db[ACCESS_LOG].insert_one({
            "institute_id": institute_id, "user_id": ObjectId(user_id) if ObjectId.is_valid(user_id) else user_id,
            "action": action, "report": report, "scope": scope, "segment": segment, "rows": rows,
            "at": datetime.now(timezone.utc),
        })
    except Exception as exc:                                        # pragma: no cover - the message is what matters
        raise HTTPException(status_code=503, detail="The access log could not be written, so the list is not shown") from exc


async def _identify(db, institute_id: ObjectId, keys: Set[str], batch_of: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Match transcript student ids to students. The enrollment number always matches; a roll number matches only for a
    student who is in the batch of that transcript, because roll numbers repeat between batches."""
    students = [s async for s in db["studentDetails"].find({"institute_id": institute_id}) if not s.get("is_deleted")]
    by_enrollment = {_text(s.get("enrollment_no")): s for s in students if _text(s.get("enrollment_no"))}
    found = {key: by_enrollment[key] for key in keys if key in by_enrollment}
    left = keys - set(found)
    if left:
        by_roll: Dict[str, List[Dict[str, Any]]] = {}
        for s in students:
            if _text(s.get("roll_no")):
                by_roll.setdefault(_text(s["roll_no"]), []).append(s)
        batch_ids = {batch_of.get(key) for key in left if batch_of.get(key) is not None}
        in_batch: Set[Tuple[Any, Any]] = set()
        if batch_ids:
            in_batch = {(r.get("student_id"), r.get("batch_id"))
                        async for r in db[RELATION_COLLECTION].find({"batch_id": {"$in": list(batch_ids)}})}
        for key in left:
            matches = [s for s in by_roll.get(key, []) if (s["_id"], batch_of.get(key)) in in_batch]
            if len(matches) == 1:
                found[key] = matches[0]
    return found


async def build_backlog(db, institute_id: ObjectId, programme: Optional[str] = None, batch: Optional[str] = None,
                        semester: Optional[int] = None, segment: str = "all",
                        limit: Optional[int] = LIST_LIMIT) -> Dict[str, Any]:
    if segment not in SEGMENTS:
        raise HTTPException(status_code=400, detail="Invalid segment")
    ctx = await load_scope(db, institute_id, programme, batch, semester)
    batch_by_id, programme_name = ctx["batch_by_id"], ctx["programme_name"]

    # -- per student, across every semester in scope (the same figures the overview tiles use)
    per_student: Dict[str, Dict[str, Any]] = {}
    for record in sorted(ctx["records"], key=lambda r: r.get("semester_no") or 0):
        key = _text(record.get("student_id"))
        entry = per_student.setdefault(key, {"failed": 0, "subjects": [], "cgpa": None, "batch_id": None})
        entry["batch_id"] = record.get("batch_id")
        cgpa = _number(record.get("cgpa"))
        if cgpa is not None:
            entry["cgpa"] = cgpa                                    # the latest semester's cumulative CGPA wins
        entry["failed"] += _failed(record)
        for subject in record.get("subjects") or []:
            if isinstance(subject, dict) and str(subject.get("grade") or "").strip().upper() in FAIL_GRADES:
                entry["subjects"].append({"semester": record.get("semester_no"), "subject": subject.get("subject")})

    for entry in per_student.values():
        entry["backlog"] = entry["failed"] >= 1
        entry["at_risk"] = (entry["failed"] >= AT_RISK_MIN_FAILED_PAPERS
                            or (entry["cgpa"] is not None and entry["cgpa"] < AT_RISK_MIN_CGPA))
    backlog_students = [k for k, e in per_student.items() if e["backlog"]]
    at_risk_students = [k for k, e in per_student.items() if e["at_risk"]]

    wanted = [k for k, e in per_student.items()
              if (segment == "all" and (e["backlog"] or e["at_risk"]))
              or (segment == "backlog" and e["backlog"]) or (segment == "at-risk" and e["at_risk"])]

    # -- the mandatory exams not cleared (only students who already have an answer row can be listed)
    subject_by_id = {s["_id"]: s for s in ctx["subjects"]}
    mandatory = {e["_id"]: e for e in ctx["exams"] if e.get("is_mandatory")}
    uncleared: List[Dict[str, Any]] = []
    for answer in ctx["answers"]:
        exam = mandatory.get(answer.get("exam_id"))
        if exam is None:
            continue
        status = attendance_status(answer)
        maximum, final = answer.get("total_max_marks") or 0, answer.get("total_final_marks")
        percentage = None
        if status == "present" and final is not None and maximum > 0 and not pending_review(answer):
            percentage = round(final / maximum * 100, 2)
        if not is_not_cleared(exam, status, percentage):
            continue
        uncleared.append({
            "student_id": answer_student_id(answer), "name": answer.get("student_name"),
            "exam": exam.get("exam_title") or exam.get("folder_name"),
            "subject": (subject_by_id.get(exam.get("subject_id")) or {}).get("subject_name"),
            "semester": exam.get("semester"), "reason": status if status != "present" else "below_minimum",
            "percentage": percentage, "minimum": exam.get("min_clear_percent"),
        })

    identified = await _identify(db, institute_id, set(wanted) | {r["student_id"] for r in uncleared},
                                 {k: per_student[k]["batch_id"] for k in per_student})

    def row(key: str) -> Dict[str, Any]:
        e, student = per_student[key], identified.get(key)
        batch_doc = batch_by_id.get(e["batch_id"]) or {}
        reasons = []
        if e["failed"]:
            reasons.append({"code": "failed_papers", "value": e["failed"]})
        if e["cgpa"] is not None and e["cgpa"] < AT_RISK_MIN_CGPA:
            reasons.append({"code": "low_cgpa", "value": e["cgpa"]})
        return {
            "student_id": key, "name": (student or {}).get("name"),
            "programme": programme_name.get(batch_doc.get("programme_id")), "batch": batch_doc.get("batch_name"),
            "failed": e["failed"], "subjects": e["subjects"][:SUBJECTS_SHOWN],
            "more_subjects": max(len(e["subjects"]) - SUBJECTS_SHOWN, 0), "cgpa": e["cgpa"],
            "backlog": e["backlog"], "at_risk": e["at_risk"], "reasons": reasons,
        }

    rows = sorted((row(k) for k in wanted), key=lambda r: (-r["failed"], r["cgpa"] if r["cgpa"] is not None else 99,
                                                           _text(r["name"]), r["student_id"]))
    uncleared_rows = sorted(({**u, "name": u["name"] or (identified.get(u["student_id"]) or {}).get("name")}
                             for u in uncleared),
                            key=lambda r: (_text(r["exam"]), _text(r["name"]), r["student_id"]))
    cap = len(rows) if limit is None else limit
    return {
        "scope": scope_summary(ctx), "filters": scope_filters(ctx), "segment": segment,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "labels": {"backlog": backlog_label_of(ctx["institute"])},
        "rules": {"fail_grades": sorted(FAIL_GRADES), "pass_target_percent": PASS_TARGET_PERCENT,
                  "at_risk_failed_papers": AT_RISK_MIN_FAILED_PAPERS, "at_risk_min_cgpa": AT_RISK_MIN_CGPA},
        "summary": {
            "with_results": len(per_student), "backlog_students": len(backlog_students),
            "backlog_papers": sum(e["failed"] for e in per_student.values()),
            "at_risk_students": len(at_risk_students), "listed": len(rows), "not_cleared": len(uncleared_rows),
        },
        "students": {"items": rows[:cap], "total": len(rows), "truncated": len(rows) > cap},
        "not_cleared": {"items": uncleared_rows[:cap], "total": len(uncleared_rows), "truncated": len(uncleared_rows) > cap},
    }


def reasons_text(reasons: List[Dict[str, Any]]) -> str:
    parts = []
    for r in reasons:
        if r["code"] == "failed_papers":
            parts.append(f"{r['value']} failed subject{'s' if r['value'] != 1 else ''}")
        elif r["code"] == "low_cgpa":
            parts.append(f"CGPA {r['value']}, below {AT_RISK_MIN_CGPA}")
    return "; ".join(parts)


def build_workbook(report: Dict[str, Any], institute_name: str, generated_by: str) -> bytes:
    import io
    from openpyxl import Workbook

    label = report["labels"]["backlog"]
    book = Workbook()
    ws = book.active
    ws.title = "Students"
    _write_table(ws, ["Student", "Enrollment / roll no", "Programme", "Batch", f"Failed subjects ({label})", "Which subjects",
                      "Latest CGPA", label, "At risk", "Reason"],
                 [[s["name"], s["student_id"], s["programme"], s["batch"], s["failed"],
                   "; ".join(f"Sem {x['semester']} {x['subject']}" for x in s["subjects"])
                   + (f" and {s['more_subjects']} more" if s["more_subjects"] else ""),
                   s["cgpa"], "Yes" if s["backlog"] else "No", "Yes" if s["at_risk"] else "No",
                   reasons_text(s["reasons"])] for s in report["students"]["items"]])

    ws = book.create_sheet("Not cleared")
    reason_words = {"absent": "Absent", "malpractice": "Malpractice", "below_minimum": "Below the minimum"}
    _write_table(ws, ["Student", "Enrollment / roll no", "Mandatory exam", "Subject", "Semester", "Reason", "Marks %",
                      "Minimum %"],
                 [[u["name"], u["student_id"], u["exam"], u["subject"], u["semester"], reason_words.get(u["reason"], u["reason"]),
                   u["percentage"], u["minimum"]] for u in report["not_cleared"]["items"]])

    rules, summary = report["rules"], report["summary"]
    info = [
        ("Report", "Backlog and At-risk"),
        ("Institute", institute_name),
        ("Scope", scope_words(report)),
        ("List shown", {"all": "Backlog or at risk", "backlog": label + " only", "at-risk": "At risk only"}[report["segment"]]),
        ("Students in the list", summary["listed"]),
        ("CONTAINS STUDENT NAMES", "Yes. Handle as confidential student data."),
        ("Generated at (UTC)", report["generated_at"]),
        ("Generated by", generated_by),
        ("Access", "This export was recorded in the report access log."),
        (label, "A subject with grade " + ", ".join(rules["fail_grades"])
         + " in any semester. Attempt history is not stored, so a cleared one is not removed."),
        ("At risk", f"{rules['at_risk_failed_papers']} or more failed subjects, or a latest CGPA under {rules['at_risk_min_cgpa']}."),
        ("Not cleared", "A mandatory exam where the student was absent, caught in malpractice, or below the minimum %. "
                        "Only students with a saved answer sheet can be listed."),
        ("Left out", "Results marked as needing regeneration."),
    ]
    ws = book.create_sheet("Report info")
    for r, (name, value) in enumerate(info, 1):
        _text_cell(ws, r, 1, name, bold=True)
        _text_cell(ws, r, 2, value)
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 90
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
