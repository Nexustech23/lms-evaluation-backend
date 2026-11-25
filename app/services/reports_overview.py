# ============================================================
# Reports overview: the numbers for the Institute Admin's Reports dashboard (college institutes only).
#
# Read-only. It reads the institute's own records, never writes, and never calls an AI service.
# A number the app cannot work out is returned as None with a short reason code, so the screen can show "-"
# and never an invented 0. Rules that vary between institutes (what counts as a fail, who is at risk, the pass
# target) are constants at the top, so they can become settings later.
# ============================================================

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set

from bson import ObjectId
from fastapi import HTTPException

from app.models.institute import institute_type_of
from app.models.transcript import TRANSCRIPT_COLLECTION
from app.services.evaluation_review import pending_review

FAIL_GRADES = {"U", "F"}                # a subject with one of these grades is a backlog
PASS_TARGET_PERCENT = 60.0              # a programme under this pass % is listed under "needs attention"
AT_RISK_MIN_FAILED_PAPERS = 2           # at risk: this many failed subjects ...
AT_RISK_MIN_CGPA = 5.0                  # ... or a latest CGPA under this
GRADE_ORDER = ["O", "A+", "A", "B+", "B", "C", "P", "D", "E", "F", "U"]
RELATION_COLLECTION = "StudentSubjectRelationModel"
# What an institute calls a failed subject carried forward. The admin chooses; the default is the plain word.
BACKLOG_LABELS = ("Backlog", "Arrear", "Back paper", "ATKT", "Supplementary")
DEFAULT_BACKLOG_LABEL = "Backlog"


# ---- small helpers ----------------------------------------------------------------------------------------------------

def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _percent(part: int, whole: int) -> Optional[float]:
    return round(part / whole * 100, 1) if whole else None


def _mean(values: List[float]) -> Optional[float]:
    return round(sum(values) / len(values), 2) if values else None


def backlog_label_of(institute: Optional[Dict[str, Any]]) -> str:
    value = (institute or {}).get("reports_backlog_label")
    return value if value in BACKLOG_LABELS else DEFAULT_BACKLOG_LABEL


def _object_id(value: Optional[str], name: str) -> Optional[ObjectId]:
    if value in (None, "", "undefined"):
        return None
    if not ObjectId.is_valid(str(value)):
        raise HTTPException(status_code=400, detail=f"Invalid {name}")
    return ObjectId(str(value))


def _tile(value: Any = None, of: Any = None, previous: Any = None, reason: Optional[str] = None,
          **extra: Any) -> Dict[str, Any]:
    """One number for the screen: value is None when it cannot be worked out, and reason says why."""
    return {"value": value, "of": of, "previous": previous, "reason": None if value is not None else reason, **extra}


def _grades(record: Dict[str, Any]) -> List[str]:
    """The upper-case grades of a transcript record's subjects; subjects without a grade are left out."""
    grades = []
    for subject in record.get("subjects") or []:
        if isinstance(subject, dict):
            grade = str(subject.get("grade") or "").strip().upper()
            if grade:
                grades.append(grade)
    return grades


def _failed(record: Dict[str, Any]) -> int:
    return sum(1 for grade in _grades(record) if grade in FAIL_GRADES)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


def _ids(docs: Iterable[Dict[str, Any]], key: str = "_id") -> List[Any]:
    return [doc[key] for doc in docs if doc.get(key) is not None]


def _pass_summary(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Appeared, passed and pass % for transcript records (one record is one student's semester)."""
    graded = [r for r in records if _grades(r)]
    passed = sum(1 for r in graded if _failed(r) == 0)
    return {"n": len(graded), "passed": passed, "pass_pct": _percent(passed, len(graded))}


def _mean_cgpa(records: List[Dict[str, Any]]) -> Optional[float]:
    return _mean([c for c in (_number(r.get("cgpa")) for r in records) if c is not None])


# ---- the overview ------------------------------------------------------------------------------------------------------

async def load_scope(db, institute_id: ObjectId, programme: Optional[str] = None, batch: Optional[str] = None,
                     semester: Optional[int] = None, with_exams: bool = True) -> Dict[str, Any]:
    """Everything every Reports page needs for one scope: the institute's programmes and batches, the transcripts,
    courses, exams and answer sheets inside the scope, and the semester that is in force. College institutes only."""
    institute = await db["instituteDetails"].find_one({"_id": institute_id})
    if not institute:
        raise HTTPException(status_code=400, detail="Institute profile not found")
    if institute_type_of(institute) != "college":
        raise HTTPException(status_code=403, detail="Reports are available for colleges only")

    programme_id, batch_id = _object_id(programme, "programme"), _object_id(batch, "batch")
    if semester is not None and semester < 1:
        raise HTTPException(status_code=400, detail="Invalid semester")

    # -- what the screen can filter by
    programmes = [p async for p in db["programmeDetails"].find({"institute_id": institute_id})]
    batches = [b async for b in db["batchDetails"].find({"institute_id": institute_id}) if not b.get("deleted")]
    if batch_id and batch_id not in _ids(batches):
        raise HTTPException(status_code=404, detail="Batch not found")
    batch_by_id = {b["_id"]: b for b in batches}
    programme_name = {p["_id"]: p.get("programme_name") for p in programmes}

    scoped_batches = [b for b in batches
                      if (not programme_id or b.get("programme_id") == programme_id)
                      and (not batch_id or b["_id"] == batch_id)]
    scoped_batch_ids: Optional[Set[Any]] = set(_ids(scoped_batches)) if (programme_id or batch_id) else None

    # -- transcripts in scope (results that need regenerating are left out)
    every_record = [r async for r in db[TRANSCRIPT_COLLECTION].find({"institute_id": institute_id})
                    if scoped_batch_ids is None or r.get("batch_id") in scoped_batch_ids]
    records = [r for r in every_record if not r.get("is_stale")]
    stale_results = len({(r.get("student_id"), r.get("semester_no")) for r in every_record if r.get("is_stale")})

    semesters_present = sorted({r["semester_no"] for r in records if isinstance(r.get("semester_no"), int)})
    semester_defaulted = semester is None
    if semester is None and semesters_present:
        semester = semesters_present[-1]
    semester_records = [r for r in records if semester is not None and r.get("semester_no") == semester]
    previous_records = [r for r in records if semester is not None and r.get("semester_no") == semester - 1]

    # -- courses, exams, answer sheets
    subjects = [s async for s in db["subjectDetails"].find({"institute_id": institute_id}) if not s.get("is_deleted")
                and (not programme_id or s.get("programme_id") == programme_id)
                and (not batch_id or s.get("batch_id") == batch_id)]
    subject_ids = _ids(subjects)
    exams = ([e async for e in db["newsavedDocs"].find({"subject_id": {"$in": subject_ids}})
              if not e.get("is_archived") and (semester is None or e.get("semester") == semester)]
             if with_exams and subject_ids else [])
    exam_ids = _ids(exams)
    answers = [a async for a in db["answerDetails"].find({"exam_id": {"$in": exam_ids}})] if exam_ids else []
    pending_answers = sum(1 for a in answers if pending_review(a))

    return {
        "institute": institute, "programme_id": programme_id, "batch_id": batch_id, "programmes": programmes,
        "batches": batches, "batch_by_id": batch_by_id, "programme_name": programme_name,
        "every_record": every_record, "records": records, "stale_results": stale_results,
        "semesters_present": semesters_present, "semester": semester, "semester_defaulted": semester_defaulted,
        "semester_records": semester_records, "previous_records": previous_records,
        "subjects": subjects, "exams": exams, "answers": answers, "pending_answers": pending_answers,
    }


def scope_filters(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """What the scope bar can choose from."""
    batches = ctx["batches"]
    totals = [int(b["total_semesters"]) for b in batches if _number(b.get("total_semesters")) is not None]
    return {
        "programmes": [{"id": str(p["_id"]), "name": p.get("programme_name")} for p in ctx["programmes"]],
        "batches": [{"id": str(b["_id"]), "name": b.get("batch_name"),
                     "programme_id": str(b["programme_id"]) if b.get("programme_id") else None} for b in batches],
        "semesters": list(range(1, max([*ctx["semesters_present"], *totals, 0]) + 1)),
    }


def scope_summary(ctx: Dict[str, Any]) -> Dict[str, Any]:
    return {"programme": str(ctx["programme_id"]) if ctx["programme_id"] else None,
            "batch": str(ctx["batch_id"]) if ctx["batch_id"] else None,
            "semester": ctx["semester"], "semester_defaulted": ctx["semester_defaulted"]}


async def build_overview(db, institute_id: ObjectId, programme: Optional[str] = None, batch: Optional[str] = None,
                         semester: Optional[int] = None) -> Dict[str, Any]:
    ctx = await load_scope(db, institute_id, programme, batch, semester)
    programme_id, batch_id, semester = ctx["programme_id"], ctx["batch_id"], ctx["semester"]
    batch_by_id = ctx["batch_by_id"]
    programme_name, records, stale_results = ctx["programme_name"], ctx["records"], ctx["stale_results"]
    semester_records, previous_records = ctx["semester_records"], ctx["previous_records"]
    subjects, exams, answers, pending_answers = ctx["subjects"], ctx["exams"], ctx["answers"], ctx["pending_answers"]

    # -- students
    students = [s async for s in db["studentDetails"].find({"institute_id": institute_id}) if not s.get("is_deleted")
                and (not programme_id or s.get("programme_id") == programme_id)]
    if batch_id:
        in_batch = {r.get("student_id") async for r in db[RELATION_COLLECTION].find({"batch_id": batch_id})}
        students = [s for s in students if s["_id"] in in_batch]

    # -- per-student results across every semester in scope
    by_student: Dict[str, Dict[str, Any]] = {}
    for record in sorted(records, key=lambda r: r.get("semester_no") or 0):
        entry = by_student.setdefault(str(record.get("student_id")), {"failed": 0, "cgpa": None})
        entry["failed"] += _failed(record)
        cgpa = _number(record.get("cgpa"))
        if cgpa is not None:
            entry["cgpa"] = cgpa                                    # the latest semester's cumulative CGPA wins
    with_results = len(by_student)
    backlog_students = [s for s in by_student.values() if s["failed"] >= 1]
    at_risk = [s for s in by_student.values()
               if s["failed"] >= AT_RISK_MIN_FAILED_PAPERS or (s["cgpa"] is not None and s["cgpa"] < AT_RISK_MIN_CGPA)]

    # -- the tiles
    current, previous = _pass_summary(semester_records), _pass_summary(previous_records)
    courses_without_co_po = sum(1 for s in subjects if not s.get("co_po_matrix"))
    exams_without_cos = sum(1 for e in exams if not e.get("covered_cos"))
    checks_ran = bool(subjects or exams)
    tiles = {
        "students": _tile(len(students)),
        "exams": _tile(len(exams)),
        "evaluation": _tile(len(answers) - pending_answers if answers else None, of=len(answers),
                            pending=pending_answers, reason="no_answers"),
        "data_checks": _tile(courses_without_co_po + exams_without_cos + stale_results if checks_ran else None,
                             reason="nothing_to_check", courses=courses_without_co_po, exams=exams_without_cos,
                             stale=stale_results),
        "pass_percent": _tile(current["pass_pct"], of=current["n"], previous=previous["pass_pct"],
                              reason="no_results", passed=current["passed"]),
        "average_cgpa": _tile(_mean_cgpa(semester_records), previous=_mean_cgpa(previous_records),
                              reason="no_results"),
        "backlogs": _tile(len(backlog_students) if with_results else None, of=with_results, reason="no_results",
                          papers=sum(s["failed"] for s in by_student.values())),
        "at_risk": _tile(len(at_risk) if with_results else None, of=with_results, reason="no_results"),
    }

    # -- the pass % trend, for one cohort so that semester 1 .. n follow the same students
    cohort_batches = {r.get("batch_id") for r in records if r.get("batch_id") is not None}
    trend_batch = batch_id
    if trend_batch is None and len(cohort_batches) > 1:
        trend_batch = max(cohort_batches, key=lambda b: (batch_by_id.get(b) or {}).get("created_at") or datetime.min)
    elif trend_batch is None and len(cohort_batches) == 1:
        trend_batch = next(iter(cohort_batches))
    trend_records = [r for r in records if trend_batch is None or r.get("batch_id") == trend_batch]
    pass_trend = []
    for number in sorted({r["semester_no"] for r in trend_records if isinstance(r.get("semester_no"), int)}):
        summary = _pass_summary([r for r in trend_records if r["semester_no"] == number])
        if summary["n"]:
            pass_trend.append({"semester": number, **summary})

    # -- the grade distribution of the selected semester
    counts: Dict[str, int] = {}
    for record in semester_records:
        for grade in _grades(record):
            counts[grade] = counts.get(grade, 0) + 1
    total_grades = sum(counts.values())
    ordered = sorted(counts, key=lambda g: (GRADE_ORDER.index(g) if g in GRADE_ORDER else len(GRADE_ORDER), g))
    grade_distribution = [{"grade": g, "count": counts[g], "percent": _percent(counts[g], total_grades)}
                          for g in ordered]

    # -- the "needs attention" list, in the order an admin works through it
    low_pass = []
    for programme_key in {(batch_by_id.get(r.get("batch_id")) or {}).get("programme_id") for r in semester_records}:
        if programme_key is None:
            continue
        summary = _pass_summary([r for r in semester_records
                                 if (batch_by_id.get(r.get("batch_id")) or {}).get("programme_id") == programme_key])
        if summary["n"] and summary["pass_pct"] < PASS_TARGET_PERCENT:
            low_pass.append(programme_name.get(programme_key) or "-")
    attention = [item for item in (
        {"key": "no_co_mapping", "count": courses_without_co_po, "of": len(subjects), "report": "data-check"},
        {"key": "exams_without_cos", "count": exams_without_cos, "of": len(exams), "report": "data-check"},
        {"key": "stale_results", "count": stale_results, "report": "data-check"},
        {"key": "evaluation_pending", "count": pending_answers, "of": len(answers), "report": "data-check"},
        {"key": "low_pass_programmes", "count": len(low_pass), "names": sorted(low_pass)[:3],
         "report": "result-analysis"},
        {"key": "at_risk", "count": len(at_risk) if with_results else 0, "report": "backlog-at-risk"},
    ) if item["count"]]

    dates = [d for d in (r.get("updated_at") or r.get("created_at") for r in records) if isinstance(d, datetime)]
    return {
        "scope": scope_summary(ctx),
        "as_of": _iso(max(dates)) if dates else None,
        "filters": scope_filters(ctx),
        "coverage": {"students": len(students), "with_results": with_results},
        "labels": {"backlog": backlog_label_of(ctx["institute"])},
        "rules": {"fail_grades": sorted(FAIL_GRADES), "pass_target_percent": PASS_TARGET_PERCENT,
                  "at_risk_failed_papers": AT_RISK_MIN_FAILED_PAPERS, "at_risk_min_cgpa": AT_RISK_MIN_CGPA},
        "tiles": tiles,
        "pass_trend": {"batch": (batch_by_id.get(trend_batch) or {}).get("batch_name") if trend_batch else None,
                       "points": pass_trend},
        "grade_distribution": {"semester": semester, "items": grade_distribution, "total": total_grades},
        "attention": {"items": attention, "all_clear": checks_ran and not attention},
    }
