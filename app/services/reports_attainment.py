# ============================================================
# CO / PO Attainment summary for the Reports dashboard: for every course in the scope, how far each course outcome (CO) was
# attained, and from that how far each programme outcome (PO) was attained, rolled up by programme and by batch.
# Read-only, college institutes only, no AI, no student named.
#
# THE MATHS IS NOT RE-INVENTED. A course's CO scores come from the same building blocks as the per-course Excel report
# (services/co_excel.py): the same exam fetch (absent and malpractice students left out), the same programme level bands
# and CO target, the same grouping of exams into categories with their weights, and the same per-category and final CO
# sheets, run on a throwaway workbook so the numbers cannot drift from the Excel report. PO attainment repeats the Excel
# report's formulas: PO % = sum of the CO-PO weights of the COs that were attained / sum of all the weights for that PO.
#
# A course that cannot be worked out is listed with the reason (no answer sheets, review pending, no CO-PO matrix, no
# programme level bands) and is left out of the roll-ups, never counted as zero.
# ============================================================

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import HTTPException
from openpyxl import Workbook

from app.services import co_excel
from app.services.reports_overview import load_scope, scope_filters, scope_summary
from app.services.reports_result_analysis import _text_cell, _write_table, scope_words

MAX_SUBJECTS = 40                       # courses worked out per request; more than this asks the admin to narrow the scope
PO_LEVEL_SCALE = 3                      # the Excel report's PO level is the PO % on a scale of 0 to 3


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def final_co_scores(exams: List[Dict[str, Any]], targets: List[Dict[str, Any]], co_target: float,
                    matrix: Dict[str, Any], course: Dict[str, Any]):
    """The final CO attainment per CO, exactly as the per-course Excel report works it out (sync; run off the event loop)."""
    all_cos = sorted({co["co_code"] for exam in exams for student in exam.get("students", [])
                      for q in student.get("questions", []) for co in q.get("cos", [])})
    book = Workbook()
    book.remove(book.active)
    categories: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for exam in exams:
        category = co_excel.exam_category(exam)
        if category not in categories:
            categories[category] = {"members": [], "is_indirect": bool(exam.get("is_course_exit_summary"))}
            order.append(category)
        categories[category]["members"].append(exam)
    known = [label for _, label in co_excel._CATEGORY_PREFIXES]
    if "MOOCs" not in categories:
        categories["MOOCs"] = {"members": [], "is_indirect": False}
        order.append("MOOCs")
    order = sorted(order, key=lambda c: known.index(c) if c in known else 999)
    weights = {c: round(sum(m.get("weightage", 0) or 0 for m in categories[c]["members"]), 3) for c in order}
    reports = []
    for category in order:
        per_co, sheet = co_excel.build_category_sheet(book, category, categories[category]["members"], targets,
                                                      co_target, course.get("course", ""))
        reports.append({"category": category, "sheet_name": sheet, "weight": weights[category],
                        "is_indirect": categories[category]["is_indirect"], "per_co": per_co})
    scores, _ = co_excel.build_final_co_attainment_sheet(book, course, reports, all_cos, co_target, matrix)
    return all_cos, scores


def po_attainment(all_cos: List[str], scores: Dict[str, float], co_target: float,
                  matrix: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    """PO % and PO level, by the Excel report's formulas. A PO with no weights at all is left out."""
    result: Dict[str, Dict[str, float]] = {}
    for po in co_excel.po_codes_from_matrix(matrix):
        total = attained = 0.0
        for co in all_cos:
            weight = _number(((matrix.get(co) or {}).get(po)))
            if weight is None:
                continue
            total += weight
            if (scores.get(co) or 0) >= co_target:
                attained += weight
        if total > 0:
            result[po] = {"percent": round(attained / total * 100, 2),
                          "level": round(round(attained / total * 100, 2) / 100 * PO_LEVEL_SCALE, 2)}
    return result


async def subject_attainment(db, subject: Dict[str, Any]) -> Dict[str, Any]:
    """One course: its status, its COs against the target, and its POs."""
    sid = str(subject["_id"])
    matrix = subject.get("co_po_matrix") or {}
    row: Dict[str, Any] = {"id": sid, "name": subject.get("subject_name"), "code": subject.get("subject_code"),
                           "programme_id": subject.get("programme_id"), "batch_id": subject.get("batch_id"),
                           "semester": subject.get("semester"), "status": "ok", "co": [], "po": {}}
    try:
        exams = await co_excel.fetch_exams(db, sid)
    except HTTPException as exc:
        if exc.status_code == 409:                                    # a faculty review is still pending
            return {**row, "status": "pending_review"}
        raise
    if not any(exam["students"] for exam in exams):
        return {**row, "status": "no_data"}
    targets, co_target = await co_excel.fetch_targets(db, sid)
    if not targets:
        return {**row, "status": "no_targets"}
    course = {"course": subject.get("subject_name") or "", "course_code": subject.get("subject_code") or "",
              "semester": subject.get("semester") or "", "co_descriptions": {}, "branch": "", "faculty_display": ""}
    all_cos, scores = await asyncio.to_thread(final_co_scores, exams, targets, co_target, matrix, course)
    row["co"] = [{"code": co, "score": round(scores.get(co) or 0, 2), "target": co_target,
                  "attained": (scores.get(co) or 0) >= co_target} for co in all_cos]
    if not matrix:
        return {**row, "status": "no_matrix"}
    row["po"] = po_attainment(all_cos, scores, co_target, matrix)
    return row


def _roll_up(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Averages over the courses that have a value: a PO is the mean of the PO % of every course that addresses it."""
    per_po: Dict[str, List[Dict[str, float]]] = {}
    attained = total = 0
    for r in rows:
        attained += sum(1 for c in r["co"] if c["attained"])
        total += len(r["co"])
        for po, value in r["po"].items():
            per_po.setdefault(po, []).append(value)
    return {
        "subjects": len(rows), "co_attained": attained, "co_total": total,
        "po": {po: {"percent": round(sum(v["percent"] for v in vals) / len(vals), 1),
                    "level": round(sum(v["level"] for v in vals) / len(vals), 2), "subjects": len(vals)}
               for po, vals in per_po.items()},
    }


async def build_attainment(db, institute_id: ObjectId, programme: Optional[str] = None, batch: Optional[str] = None,
                           semester: Optional[int] = None) -> Dict[str, Any]:
    ctx = await load_scope(db, institute_id, programme, batch, semester, with_exams=False)
    batch_by_id, programme_name = ctx["batch_by_id"], ctx["programme_name"]
    # The report follows the semester only when the admin chose one; it does not default to the latest.
    subjects = [s for s in ctx["subjects"] if semester is None or s.get("semester") == semester]
    base = {
        "scope": {**scope_summary(ctx), "semester": semester, "semester_defaulted": False},
        "filters": scope_filters(ctx), "generated_at": datetime.now(timezone.utc).isoformat(),
        "limit": MAX_SUBJECTS, "subjects_in_scope": len(subjects),
    }
    if len(subjects) > MAX_SUBJECTS:
        return {**base, "too_many": True, "counts": {}, "po_codes": [], "programmes": [], "batches": [], "subjects": []}

    rows = sorted([await subject_attainment(db, s) for s in subjects],
                  key=lambda r: (str(programme_name.get(r["programme_id"]) or ""), r["semester"] or 0, str(r["name"] or "")))
    usable = [r for r in rows if r["status"] in ("ok", "no_matrix")]
    po_rows = [r for r in rows if r["status"] == "ok"]
    counts = {"in_scope": len(rows), "computed": len(usable), "with_po": len(po_rows)}
    for status in ("no_data", "pending_review", "no_matrix", "no_targets"):
        counts[status] = sum(1 for r in rows if r["status"] == status)

    def group(key: str, names) -> List[Dict[str, Any]]:
        out = []
        for gid in {r[key] for r in usable if r[key] is not None}:
            members = [r for r in usable if r[key] == gid]
            out.append({"id": str(gid), "name": names(gid), **_roll_up(members)})
        return sorted(out, key=lambda g: str(g["name"] or ""))

    programmes = group("programme_id", lambda g: programme_name.get(g))
    batches = group("batch_id", lambda g: (batch_by_id.get(g) or {}).get("batch_name"))
    po_codes = sorted({po for r in po_rows for po in r["po"]}, key=co_excel._po_sort_key)
    return {
        **base, "too_many": False, "counts": counts, "po_codes": po_codes, "programmes": programmes, "batches": batches,
        "overall": _roll_up(usable),
        "subjects": [{"id": r["id"], "name": r["name"], "code": r["code"], "status": r["status"], "semester": r["semester"],
                      "programme": programme_name.get(r["programme_id"]),
                      "batch": (batch_by_id.get(r["batch_id"]) or {}).get("batch_name"), "co": r["co"], "po": r["po"]}
                     for r in rows],
    }


def build_workbook(report: Dict[str, Any], institute_name: str, generated_by: str) -> bytes:
    import io
    po_codes = report["po_codes"]
    book = Workbook()

    def po_table(ws, label: str, groups: List[Dict[str, Any]]):
        _write_table(ws, [label, "Courses counted", "COs attained", "COs total", *[f"{po} %" for po in po_codes]],
                     [[g["name"], g["subjects"], g["co_attained"], g["co_total"],
                       *[(g["po"].get(po) or {}).get("percent") for po in po_codes]] for g in groups])

    ws = book.active
    ws.title = "Programmes"
    po_table(ws, "Programme", report["programmes"])
    po_table(book.create_sheet("Batches"), "Batch", report["batches"])

    ws = book.create_sheet("Courses")
    words = {"ok": "Worked out", "no_matrix": "No CO-PO matrix", "no_data": "No answer sheets",
             "pending_review": "Review pending", "no_targets": "No programme level bands"}
    _write_table(ws, ["Course", "Code", "Programme", "Batch", "Semester", "Status", "COs (score / target)", *[f"{po} %" for po in po_codes]],
                 [[s["name"], s["code"], s["programme"], s["batch"], s["semester"], words.get(s["status"], s["status"]),
                   "; ".join(f"{c['code']} {c['score']}/{c['target']} {'attained' if c['attained'] else 'not attained'}" for c in s["co"]),
                   *[(s["po"].get(po) or {}).get("percent") for po in po_codes]] for s in report["subjects"]])

    counts = report["counts"]
    info = [
        ("Report", "CO / PO Attainment"), ("Institute", institute_name), ("Scope", scope_words(report, "All semesters")),
        ("Courses in scope", counts.get("in_scope")), ("Courses worked out", counts.get("computed")),
        ("Generated at (UTC)", report["generated_at"]), ("Generated by", generated_by),
        ("CO attainment", "Same rules as the per-course Excel report: each exam's level comes from the share of students at or "
                          "above the threshold (level bands set on the programme), weighted by the exam weight; the final CO "
                          "score adds the direct categories and the course-exit (indirect) category by weight. A CO is "
                          "attained when its final score reaches the CO target."),
        ("PO attainment", "PO % = the CO-PO weights of the attained COs / all the CO-PO weights for that PO x 100. "
                          "Level = PO % / 100 x 3. A programme's or batch's PO % is the mean over its courses that address the PO."),
        ("Left out", "Absent and malpractice students; courses with no answer sheets, a pending review, no CO-PO matrix "
                     "(for PO) or no programme level bands. Courses left out are listed, never counted as zero."),
        ("Limits", "No PO target is stored in the app, so PO values are shown without a target. No student is named."),
    ]
    ws = book.create_sheet("Report info")
    for r, (name, value) in enumerate(info, 1):
        _text_cell(ws, r, 1, name, bold=True)
        _text_cell(ws, r, 2, value)
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 100
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
