# ============================================================
# Result Analysis report: how a semester's results turned out, by programme, subject, grade and CGPA. Read-only, college
# institutes only, no AI. It uses the same scope and the same pass rule as the Reports overview, so the numbers match.
#
# Students are never named here: every row is a count, an average or a percentage. The Excel export carries the same
# numbers plus a "Report info" sheet that says what the numbers cover and how they were worked out.
# ============================================================

import io
import statistics
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

from app.services.reports_overview import (
    AT_RISK_MIN_CGPA, AT_RISK_MIN_FAILED_PAPERS, FAIL_GRADES, GRADE_ORDER, PASS_TARGET_PERCENT, _failed, _grades,
    _iso, _mean, _mean_cgpa, _number, _pass_summary, _percent, load_scope, scope_filters, scope_summary,
)

SMALL_GROUP = 10                         # under this many students a percentage is shown with a "small group" note
CGPA_BANDS = [(0, 4), (4, 5), (5, 6), (6, 7), (7, 8), (8, 9), (9, 10)]


def _grade_key(grade: str):
    return (GRADE_ORDER.index(grade) if grade in GRADE_ORDER else len(GRADE_ORDER), grade)


def _subject_rows(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per subject name: how many sat it, how many passed, and the spread of the marks."""
    by_subject: Dict[str, Dict[str, Any]] = {}
    for record in records:
        for subject in record.get("subjects") or []:
            if not isinstance(subject, dict):
                continue
            grade = str(subject.get("grade") or "").strip().upper()
            if not grade:
                continue
            name = str(subject.get("subject") or "").strip() or "-"
            entry = by_subject.setdefault(name, {"grades": {}, "marks": []})
            entry["grades"][grade] = entry["grades"].get(grade, 0) + 1
            marks = _number(subject.get("marks"))
            if marks is not None:
                entry["marks"].append(marks)
    rows = []
    for name, entry in by_subject.items():
        appeared = sum(entry["grades"].values())
        passed = sum(count for grade, count in entry["grades"].items() if grade not in FAIL_GRADES)
        marks = entry["marks"]
        rows.append({
            "name": name, "appeared": appeared, "passed": passed, "pass_pct": _percent(passed, appeared),
            "mean": round(sum(marks) / len(marks), 1) if marks else None,
            "median": round(statistics.median(marks), 1) if marks else None,
            "highest": max(marks) if marks else None, "lowest": min(marks) if marks else None,
            "small": appeared < SMALL_GROUP, "grades": entry["grades"],
        })
    return sorted(rows, key=lambda r: (r["pass_pct"] if r["pass_pct"] is not None else 101, r["name"]))


def _cgpa_distribution(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    values = [c for c in (_number(r.get("cgpa")) for r in records) if c is not None]
    bands = []
    for index, (low, high) in enumerate(CGPA_BANDS):
        last = index == len(CGPA_BANDS) - 1
        count = sum(1 for v in values if low <= v < high or (last and v == high))
        bands.append({"label": f"{low}-{high}", "from": low, "to": high, "count": count})
    return bands if values else []


async def build_result_analysis(db, institute_id: ObjectId, programme: Optional[str] = None,
                                batch: Optional[str] = None, semester: Optional[int] = None) -> Dict[str, Any]:
    ctx = await load_scope(db, institute_id, programme, batch, semester)
    records, previous = ctx["semester_records"], ctx["previous_records"]
    batch_by_id, programme_name = ctx["batch_by_id"], ctx["programme_name"]

    current, before = _pass_summary(records), _pass_summary(previous)
    graded = [r for r in records if _grades(r)]
    cgpas = [c for c in (_number(r.get("cgpa")) for r in graded) if c is not None]
    subjects = _subject_rows(records)
    ranked = [s for s in subjects if s["pass_pct"] is not None]

    programmes = []
    for key in {(batch_by_id.get(r.get("batch_id")) or {}).get("programme_id") for r in graded}:
        if key is None:
            continue
        rows = [r for r in graded if (batch_by_id.get(r.get("batch_id")) or {}).get("programme_id") == key]
        summary = _pass_summary(rows)
        programmes.append({
            "id": str(key), "name": programme_name.get(key) or "-", "appeared": summary["n"],
            "passed": summary["passed"], "pass_pct": summary["pass_pct"], "average_cgpa": _mean_cgpa(rows),
            "small": summary["n"] < SMALL_GROUP,
            "below_target": summary["pass_pct"] is not None and summary["pass_pct"] < PASS_TARGET_PERCENT,
        })
    programmes.sort(key=lambda p: (p["pass_pct"] if p["pass_pct"] is not None else 101, p["name"]))

    overall: Dict[str, int] = {}
    for record in graded:
        for grade in _grades(record):
            overall[grade] = overall.get(grade, 0) + 1
    total_grades = sum(overall.values())
    order = sorted(overall, key=_grade_key)
    dates = [d for d in (r.get("updated_at") or r.get("created_at") for r in records) if isinstance(d, datetime)]

    return {
        "scope": scope_summary(ctx),
        "filters": scope_filters(ctx),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": _iso(max(dates)) if dates else None,
        "summary": {
            "appeared": current["n"], "passed": current["passed"], "failed": current["n"] - current["passed"],
            "pass_pct": current["pass_pct"], "previous_pass_pct": before["pass_pct"],
            "average_cgpa": _mean(cgpas), "previous_average_cgpa": _mean_cgpa(previous),
            "highest_cgpa": max(cgpas) if cgpas else None, "lowest_cgpa": min(cgpas) if cgpas else None,
            "lowest_subject": ({"name": ranked[0]["name"], "pass_pct": ranked[0]["pass_pct"],
                                "n": ranked[0]["appeared"]} if ranked else None),
        },
        "programmes": programmes,
        "subjects": [{k: v for k, v in s.items() if k != "grades"} for s in subjects],
        "grades": {
            "order": order,
            "overall": [{"grade": g, "count": overall[g], "percent": _percent(overall[g], total_grades)} for g in order],
            "subjects": [{"name": s["name"], "counts": s["grades"]} for s in subjects],
        },
        "cgpa_distribution": _cgpa_distribution(graded),
        "rules": {"fail_grades": sorted(FAIL_GRADES), "pass_target_percent": PASS_TARGET_PERCENT,
                  "at_risk_failed_papers": AT_RISK_MIN_FAILED_PAPERS, "at_risk_min_cgpa": AT_RISK_MIN_CGPA,
                  "small_group": SMALL_GROUP},
    }


# ---- Excel export ----------------------------------------------------------------------------------------------------

def _text_cell(ws, row: int, column: int, value: Any, bold: bool = False):
    """A cell holding text. It is always stored as a string, so a name that begins with = + - or @ can never run as a
    formula when the file is opened."""
    cell = ws.cell(row=row, column=column, value=value)
    if isinstance(value, str):
        cell.data_type = "s"
    if bold:
        cell.font = Font(bold=True)
    cell.alignment = Alignment(wrap_text=True, vertical="top")
    return cell


def _write_table(ws, headers: List[str], rows: List[List[Any]]):
    for column, header in enumerate(headers, 1):
        _text_cell(ws, 1, column, header, bold=True)
    for r, values in enumerate(rows, 2):
        for column, value in enumerate(values, 1):
            _text_cell(ws, r, column, "-" if value is None else value)
    for column, header in enumerate(headers, 1):
        width = max([len(str(header))] + [len(str(row[column - 1])) for row in rows if row[column - 1] is not None])
        ws.column_dimensions[ws.cell(row=1, column=column).column_letter].width = min(max(width + 2, 10), 40)
    ws.freeze_panes = "A2"


def scope_words(report: Dict[str, Any]) -> str:
    """The scope in plain words, for the export and anywhere else a sentence is needed."""
    scope, filters = report["scope"], report["filters"]
    programme = next((p["name"] for p in filters["programmes"] if p["id"] == scope["programme"]), "All programmes")
    batch = next((b["name"] for b in filters["batches"] if b["id"] == scope["batch"]), "All batches")
    semester = f"Semester {scope['semester']}" if scope["semester"] else "No semester with results"
    return f"{semester} | {programme} | {batch}"


def build_workbook(report: Dict[str, Any], institute_name: str, generated_by: str) -> bytes:
    summary = report["summary"]
    book = Workbook()

    ws = book.active
    ws.title = "Subjects"
    _write_table(ws, ["Subject", "Appeared", "Passed", "Pass %", "Mean marks", "Median marks", "Highest", "Lowest",
                      "Small group"],
                 [[s["name"], s["appeared"], s["passed"], s["pass_pct"], s["mean"], s["median"], s["highest"],
                   s["lowest"], "Yes" if s["small"] else "No"] for s in report["subjects"]])

    ws = book.create_sheet("Programmes")
    _write_table(ws, ["Programme", "Appeared", "Passed", "Pass %", "Average CGPA", "Below target"],
                 [[p["name"], p["appeared"], p["passed"], p["pass_pct"], p["average_cgpa"],
                   "Yes" if p["below_target"] else "No"] for p in report["programmes"]])

    ws = book.create_sheet("Grades")
    order = report["grades"]["order"]
    _write_table(ws, ["Subject", *order],
                 [[s["name"], *[s["counts"].get(g, 0) for g in order]] for s in report["grades"]["subjects"]])

    ws = book.create_sheet("CGPA bands")
    _write_table(ws, ["CGPA band", "Students"], [[b["label"], b["count"]] for b in report["cgpa_distribution"]])

    rules = report["rules"]
    info = [
        ("Report", "Result Analysis"),
        ("Institute", institute_name),
        ("Scope", scope_words(report)),
        ("Students who appeared", summary["appeared"]),
        ("Pass %", summary["pass_pct"]),
        ("Data as of", report["as_of"] or "-"),
        ("Generated at (UTC)", report["generated_at"]),
        ("Generated by", generated_by),
        ("Pass rule", "A student passes a semester when no subject has a failing grade ("
                      + ", ".join(rules["fail_grades"]) + "). Pass % = passed / appeared."),
        ("Subject pass %", "Per subject: students with a non-failing grade / students graded in it."),
        ("Pass target", f"{rules['pass_target_percent']}% (programmes below it are marked)."),
        ("Small group", f"Fewer than {rules['small_group']} students: treat the percentage with care."),
        ("Left out", "Results marked as needing regeneration, and subjects without a grade."),
    ]
    ws = book.create_sheet("Report info")
    for r, (label, value) in enumerate(info, 1):
        _text_cell(ws, r, 1, label, bold=True)
        _text_cell(ws, r, 2, value)
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 90

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
