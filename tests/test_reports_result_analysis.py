"""Result Analysis report: how a semester turned out, by programme, subject, grade and CGPA. A fake database answers; no
real database, no network, no AI. Things that must hold: the numbers match the overview, only this institute's fresh
results, students never named, a name can never run as a spreadsheet formula, and nothing is written."""
import asyncio
import io

import pytest
from bson import ObjectId
from fastapi import HTTPException
from openpyxl import load_workbook

from app.api.routers import reports_overview as router
from app.services import reports_overview as ro
from app.services import reports_result_analysis as rra
from tests.test_reports_overview import (
    ADMIN_USER, BATCH, INSTITUTE, OLD_BATCH, OTHER_INSTITUTE, OTHER_PROGRAMME_BATCH, database,
)


def run(coro):
    return asyncio.run(coro)


def analysis(db, **scope):
    return run(rra.build_result_analysis(db, INSTITUTE, **scope))


def rec(student, semester, subjects, cgpa=8.0, batch=BATCH, institute=INSTITUTE, stale=False, **extra):
    """A transcript record; `subjects` is a list of (name, grade, marks)."""
    base = {"_id": ObjectId(), "student_id": student, "institute_id": institute, "batch_id": batch,
            "semester_no": semester, "cgpa": cgpa, "is_stale": stale,
            "subjects": [{"subject": n, "grade": g, "marks": m} for n, g, m in subjects]}
    base.update(extra)
    return base


def sample():
    return database([
        rec("S1", 1, [("Maths", "A", 85), ("Physics", "B", 70)], 8.5),
        rec("S2", 1, [("Maths", "B", 72), ("Physics", "U", 30)], 5.5),
        rec("S3", 1, [("Maths", "U", 28), ("Physics", "U", 25)], 3.0),
        rec("S4", 1, [("Maths", "A", 90), ("Physics", "A", 88)], 9.2),
    ])


def test_the_summary_counts_who_appeared_passed_and_failed():
    summary = analysis(sample())["summary"]
    assert (summary["appeared"], summary["passed"], summary["failed"], summary["pass_pct"]) == (4, 2, 2, 50.0)
    assert summary["average_cgpa"] == 6.55 and summary["highest_cgpa"] == 9.2 and summary["lowest_cgpa"] == 3.0
    assert summary["lowest_subject"] == {"name": "Physics", "pass_pct": 50.0, "n": 4}


def test_the_previous_semester_is_returned_for_comparison():
    db = database([rec("S1", 1, [("Maths", "U", 20)], 4.0), rec("S2", 1, [("Maths", "A", 90)], 9.0),
                   rec("S1", 2, [("Maths", "A", 80)], 6.0), rec("S2", 2, [("Maths", "A", 85)], 9.0)])
    summary = analysis(db)["summary"]
    assert summary["pass_pct"] == 100.0 and summary["previous_pass_pct"] == 50.0
    assert summary["average_cgpa"] == 7.5 and summary["previous_average_cgpa"] == 6.5


def test_each_subject_has_its_pass_percent_and_the_spread_of_its_marks_lowest_pass_first():
    rows = analysis(sample())["subjects"]
    assert [r["name"] for r in rows] == ["Physics", "Maths"]                 # 50 % before 75 %
    maths = next(r for r in rows if r["name"] == "Maths")
    assert (maths["appeared"], maths["passed"], maths["pass_pct"]) == (4, 3, 75.0)
    assert (maths["mean"], maths["median"], maths["highest"], maths["lowest"]) == (68.8, 78.5, 90.0, 28.0)
    assert maths["small"] is True and "grades" not in maths                   # 4 students: a small group


def test_a_subject_without_marks_still_counts_by_grade_and_shows_dashes_for_the_spread():
    db = database([rec("S1", 1, [("Maths", "A", None)], 8.0), rec("S2", 1, [("Maths", "U", "AB")], 4.0)])
    row = analysis(db)["subjects"][0]
    assert (row["pass_pct"], row["mean"], row["median"], row["highest"], row["lowest"]) == (50.0, None, None, None, None)


def test_programmes_are_listed_lowest_pass_first_and_flag_those_under_the_target():
    db = database([rec("S1", 1, [("Maths", "A", 80)], 8.0), rec("S2", 1, [("Maths", "A", 80)], 7.0),
                   rec("S3", 1, [("Maths", "U", 20)], 4.0, batch=OTHER_PROGRAMME_BATCH),
                   rec("S4", 1, [("Maths", "A", 80)], 6.0, batch=OTHER_PROGRAMME_BATCH)])
    rows = analysis(db)["programmes"]
    assert [(r["name"], r["pass_pct"], r["below_target"], r["average_cgpa"]) for r in rows] == [
        ("MBA", 50.0, True, 5.0), ("B.Tech CSE", 100.0, False, 7.5)]


def test_grades_are_counted_overall_and_per_subject_in_grade_order():
    grades = analysis(sample())["grades"]
    assert grades["order"] == ["A", "B", "U"]
    assert [(g["grade"], g["count"]) for g in grades["overall"]] == [("A", 3), ("B", 2), ("U", 3)]
    assert next(s for s in grades["subjects"] if s["name"] == "Physics")["counts"] == {"A": 1, "B": 1, "U": 2}


def test_cgpa_bands_include_a_perfect_ten_in_the_top_band():
    db = database([rec("S1", 1, [("M", "A", 99)], 10.0), rec("S2", 1, [("M", "A", 90)], 9.0),
                   rec("S3", 1, [("M", "A", 80)], 3.9), rec("S4", 1, [("M", "A", 80)], 6.0)])
    bands = {b["label"]: b["count"] for b in analysis(db)["cgpa_distribution"]}
    assert bands == {"0-4": 1, "4-5": 0, "5-6": 0, "6-7": 1, "7-8": 0, "8-9": 0, "9-10": 2}


def test_with_no_results_everything_is_empty_or_none_never_zero():
    out = analysis(database())
    assert out["summary"]["pass_pct"] is None and out["summary"]["average_cgpa"] is None
    assert out["summary"]["lowest_subject"] is None and out["summary"]["appeared"] == 0
    assert out["subjects"] == [] and out["programmes"] == [] and out["cgpa_distribution"] == []
    assert out["grades"]["overall"] == [] and out["as_of"] is None


def test_the_numbers_match_the_overview_and_the_scope_narrows_them():
    db = database([rec("S1", 1, [("M", "A", 80)]), rec("S2", 1, [("M", "U", 20)]),
                   rec("S3", 1, [("M", "U", 20)], batch=OTHER_PROGRAMME_BATCH)])
    assert analysis(db)["summary"]["pass_pct"] == run(ro.build_overview(db, INSTITUTE))["tiles"]["pass_percent"]["value"]
    assert analysis(db, batch=str(BATCH))["summary"]["appeared"] == 2
    assert analysis(db, programme=str(ObjectId()))["summary"]["appeared"] == 0


def test_stale_results_and_other_institutes_are_left_out():
    db = database([rec("S1", 1, [("M", "A", 80)]), rec("S2", 1, [("M", "U", 20)], stale=True),
                   rec("S3", 1, [("M", "U", 20)], institute=OTHER_INSTITUTE)])
    summary = analysis(db)["summary"]
    assert (summary["appeared"], summary["pass_pct"]) == (1, 100.0)


def test_no_student_is_named_and_nothing_is_written():
    db = sample()
    for doc in db.c["academic_transcripts"].docs:
        doc["student_name"] = "Secret Name"
    out = analysis(db)
    assert "Secret Name" not in repr(out) and "S1" not in repr(out["programmes"])
    assert all(col.updates == 0 for col in db.c.values())


def test_a_school_institute_is_refused():
    with pytest.raises(HTTPException) as error:
        analysis(database(institute_type="school"))
    assert error.value.status_code == 403


# ---- the Excel export -------------------------------------------------------------------------------------------------

def workbook_of(db, **scope):
    report = analysis(db, **scope)
    return report, load_workbook(io.BytesIO(rra.build_workbook(report, "Demo University", "A. Admin")))


def test_the_export_has_the_same_numbers_and_a_report_info_sheet():
    report, book = workbook_of(sample(), batch=str(BATCH))
    assert book.sheetnames == ["Subjects", "Programmes", "Grades", "CGPA bands", "Report info"]
    subjects = {row[0]: row for row in book["Subjects"].iter_rows(min_row=2, values_only=True)}
    assert subjects["Maths"][1:4] == (4, 3, 75.0)                          # numbers stay numbers
    info = {row[0]: row[1] for row in book["Report info"].iter_rows(values_only=True)}
    assert info["Institute"] == "Demo University" and info["Generated by"] == "A. Admin"
    assert info["Scope"] == "Semester 1 | All programmes | CSE 2023" and info["Pass %"] == 50.0
    assert "failing grade" in info["Pass rule"] and info["Data as of"] == "-"


def test_a_subject_name_that_looks_like_a_formula_is_stored_as_text():
    db = database([rec("S1", 1, [("=HYPERLINK(\"http://x\")", "A", 80), ("+1+1", "B", 70), ("-2", "A", 60),
                                 ("@SUM(A1)", "A", 50)])])
    _, book = workbook_of(db)
    names = [cell.value for cell in book["Subjects"]["A"][1:]]
    assert len(names) == 4
    assert all(cell.data_type == "s" for cell in book["Subjects"]["A"][1:])
    assert any(str(n).startswith("=") for n in names)


def test_the_export_of_an_empty_scope_is_still_a_valid_workbook():
    _, book = workbook_of(database())
    assert book["Subjects"].max_row == 1 and book["Report info"]["B3"].value == "No semester with results | All programmes | All batches"


# ---- who may ask ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("call", ["reports_result_analysis", "reports_result_analysis_export"])
@pytest.mark.parametrize("role", [1, 3, 4, 5, 6, 7, None])
def test_only_an_institute_admin_may_use_the_routes(call, role):
    with pytest.raises(HTTPException) as error:
        run(getattr(router, call)(programme=None, batch=None, semester=None,
                                  identity={"role": role, "user_id": str(ADMIN_USER)}, db=database()))
    assert error.value.status_code == 403


def test_the_export_route_returns_an_xlsx_download_for_the_admins_own_institute():
    db = database([rec("S1", 1, [("Maths", "A", 80)])])
    db.c["users"].docs[0]["fullName"] = "Demo University"
    response = run(router.reports_result_analysis_export(programme=None, batch=None, semester=None,
                                                         identity={"role": 2, "user_id": str(ADMIN_USER)}, db=db))
    assert response.media_type == router.XLSX
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="result-analysis-') and disposition.endswith('.xlsx"')
    book = load_workbook(io.BytesIO(response.body))
    assert book["Report info"]["B2"].value == "Demo University"


def test_the_report_route_returns_the_admins_own_analysis():
    out = run(router.reports_result_analysis(programme=None, batch=None, semester=None,
                                             identity={"role": 2, "user_id": str(ADMIN_USER)},
                                             db=database([rec("S1", 1, [("Maths", "A", 80)])])))
    assert out["summary"]["appeared"] == 1
