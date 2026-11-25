"""Backlog and At-risk report. A fake database answers; no real database, no network, no AI. This report NAMES students,
so the things that must hold are: every view and export is written to the access log and a log that cannot be written
stops the request, the counts equal the dashboard tiles, a roll number only matches inside its own batch, a name can
never run as a spreadsheet formula, and only an Institute Admin can reach it."""
import asyncio
import io

import pytest
from bson import ObjectId
from fastapi import HTTPException
from openpyxl import load_workbook

from app.api.routers import reports_overview as router
from app.services import reports_backlog as rb
from app.services import reports_overview as ro
from tests.test_reports_overview import ADMIN_USER, BATCH, INSTITUTE, OLD_BATCH, OTHER_INSTITUTE, database
from tests.test_reports_result_analysis import rec


def run(coro):
    return asyncio.run(coro)


def report(db, **scope):
    return run(rb.build_backlog(db, INSTITUTE, **scope))


def student(enrollment, name, roll=None, institute=INSTITUTE, **extra):
    base = {"_id": ObjectId(), "institute_id": institute, "name": name, "enrollment_no": enrollment,
            "roll_no": roll, "is_deleted": False}
    base.update(extra)
    return base


def sample():
    return database([
        rec("EN1", 1, [("Maths", "A", 80), ("Physics", "B", 70)], 8.5),              # fine: not listed
        rec("EN2", 1, [("Maths", "U", 20), ("Physics", "B", 70)], 6.5),              # one backlog, not at risk
        rec("EN3", 1, [("Maths", "U", 20), ("Physics", "F", 25)], 6.0),              # two failed: at risk
        rec("EN4", 1, [("Maths", "A", 80), ("Physics", "A", 80)], 4.5),              # low CGPA: at risk, no backlog
    ], studentDetails=[student("EN1", "Asha"), student("EN2", "Bina"), student("EN3", "Chitra"), student("EN4", "Dev")])


def test_the_students_needing_follow_up_are_listed_with_names_and_reasons_most_failed_first():
    out = report(sample())
    rows = out["students"]["items"]
    assert [r["name"] for r in rows] == ["Chitra", "Bina", "Dev"]
    chitra, bina, dev = rows
    assert (chitra["failed"], chitra["backlog"], chitra["at_risk"]) == (2, True, True)
    assert chitra["reasons"] == [{"code": "failed_papers", "value": 2}]
    assert [s["subject"] for s in chitra["subjects"]] == ["Maths", "Physics"]
    assert (bina["at_risk"], bina["backlog"]) == (False, True)
    assert dev["reasons"] == [{"code": "low_cgpa", "value": 4.5}] and dev["backlog"] is False
    assert chitra["programme"] == "B.Tech CSE" and chitra["batch"] == "CSE 2023" and chitra["student_id"] == "EN3"


def test_the_segment_chooses_who_is_listed_and_a_bad_segment_is_refused():
    db = sample()
    assert [r["name"] for r in report(db, segment="backlog")["students"]["items"]] == ["Chitra", "Bina"]
    assert [r["name"] for r in report(db, segment="at-risk")["students"]["items"]] == ["Chitra", "Dev"]
    with pytest.raises(HTTPException) as error:
        report(db, segment="everyone")
    assert error.value.status_code == 400


def test_the_counts_equal_the_dashboard_tiles():
    db = sample()
    summary = report(db)["summary"]
    tiles = run(ro.build_overview(db, INSTITUTE))["tiles"]
    assert summary["backlog_students"] == tiles["backlogs"]["value"] == 2
    assert summary["backlog_papers"] == tiles["backlogs"]["papers"] == 3
    assert summary["at_risk_students"] == tiles["at_risk"]["value"] == 2
    assert summary["with_results"] == tiles["backlogs"]["of"] == 4 and summary["listed"] == 3


def test_failures_count_across_every_semester_and_the_latest_cgpa_is_used():
    db = database([rec("EN1", 1, [("M", "U", 10)], 4.0), rec("EN1", 2, [("M", "U", 10)], 8.0)],
                  studentDetails=[student("EN1", "Asha")])
    row = report(db)["students"]["items"][0]
    assert (row["failed"], row["cgpa"], row["at_risk"]) == (2, 8.0, True)
    assert [(s["semester"], s["subject"]) for s in row["subjects"]] == [(1, "M"), (2, "M")]


def test_stale_results_and_other_institutes_are_left_out():
    db = database([rec("EN1", 1, [("M", "U", 10)], stale=True), rec("EN2", 1, [("M", "U", 10)], institute=OTHER_INSTITUTE)],
                  studentDetails=[student("EN1", "Asha"), student("EN2", "Other", institute=OTHER_INSTITUTE)])
    assert report(db)["students"]["items"] == []


# ---- who a transcript id is ---------------------------------------------------------------------------------------------

def test_a_roll_number_matches_only_a_student_in_that_batch():
    mine, other = student("EN1", "Asha", roll="7"), student("EN2", "Wrong Asha", roll="7")
    relations = [{"_id": ObjectId(), "student_id": mine["_id"], "batch_id": BATCH},
                 {"_id": ObjectId(), "student_id": other["_id"], "batch_id": OLD_BATCH}]
    db = database([rec("7", 1, [("M", "U", 10)], 6.0)], studentDetails=[mine, other],
                  StudentSubjectRelationModel=relations)
    assert report(db)["students"]["items"][0]["name"] == "Asha"


def test_an_unmatched_or_ambiguous_id_shows_no_name_but_still_the_id():
    a, b = student("EN1", "A", roll="7"), student("EN2", "B", roll="7")
    both = [{"_id": ObjectId(), "student_id": s["_id"], "batch_id": BATCH} for s in (a, b)]
    db = database([rec("7", 1, [("M", "U", 10)], 6.0), rec("99", 1, [("M", "U", 10)], 6.0)],
                  studentDetails=[a, b], StudentSubjectRelationModel=both)
    rows = {r["student_id"]: r for r in report(db)["students"]["items"]}
    assert rows["7"]["name"] is None and rows["99"]["name"] is None


def test_a_deleted_student_is_not_matched():
    db = database([rec("EN1", 1, [("M", "U", 10)], 6.0)], studentDetails=[student("EN1", "Gone", is_deleted=True)])
    assert report(db)["students"]["items"][0]["name"] is None


# ---- mandatory exams not cleared ------------------------------------------------------------------------------------------

def _exam_setup():
    course = {"_id": ObjectId(), "institute_id": INSTITUTE, "programme_id": ObjectId(), "subject_name": "Maths"}
    exam = {"_id": ObjectId(), "subject_id": course["_id"], "semester": 1, "covered_cos": ["CO1"], "exam_title": "End Sem",
            "is_mandatory": True, "min_clear_percent": 40}

    def answer(sid, status=None, final=None, name=None, **extra):
        doc = {"_id": ObjectId(), "exam_id": exam["_id"], "student_id": sid, "student_name": name,
               "total_final_marks": final, "total_max_marks": 100, "evaluation_status": "evaluated"}
        if status:
            doc["attendance_status"] = status
        doc.update(extra)
        return doc
    return course, exam, answer


def test_mandatory_exams_not_cleared_are_listed_and_the_rest_are_not():
    course, exam, answer = _exam_setup()
    answers = [answer("EN1", final=65, name="Ok"), answer("EN2", final=30, name="Low"),
               answer("EN3", status="absent", name="Away"), answer("EN4", status="malpractice", name="Cheat"),
               answer("EN5", final=10, name="Pending", evaluation_status="pending_review"), answer("EN6", final=None)]
    lenient = {**exam, "_id": ObjectId(), "is_mandatory": False}
    answers.append({**answer("EN7", status="absent", name="Not mandatory"), "exam_id": lenient["_id"]})
    db = database(subjectDetails=[course], newsavedDocs=[exam, lenient], answerDetails=answers,
                  studentDetails=[student("EN6", "From Roster")])
    out = report(db)["not_cleared"]
    assert {u["name"]: u["reason"] for u in out["items"]} == {
        "Low": "below_minimum", "Away": "absent", "Cheat": "malpractice"}
    low = next(u for u in out["items"] if u["name"] == "Low")
    assert (low["exam"], low["subject"], low["percentage"], low["minimum"]) == ("End Sem", "Maths", 30.0, 40)


def test_a_not_cleared_student_without_a_name_on_the_answer_gets_it_from_the_roster():
    course, exam, answer = _exam_setup()
    db = database(subjectDetails=[course], newsavedDocs=[exam], answerDetails=[answer("EN9", status="absent")],
                  studentDetails=[student("EN9", "From Roster")])
    assert report(db)["not_cleared"]["items"][0]["name"] == "From Roster"


# ---- the access log -------------------------------------------------------------------------------------------------------

def call(name, db, user=ADMIN_USER, role=2, **extra):
    args = dict(programme=None, batch=None, semester=None, segment="all", identity={"role": role, "user_id": str(user)}, db=db)
    args.update(extra)
    return run(getattr(router, name)(**args))


def test_every_view_is_written_to_the_access_log_with_who_scope_and_row_count():
    db = sample()
    out = call("reports_backlog_at_risk", db, segment="backlog")
    entries = db[rb.ACCESS_LOG].docs
    assert len(entries) == 1 and out["students"]["total"] == 2
    entry = entries[0]
    assert (entry["action"], entry["report"], entry["segment"], entry["rows"]) == ("view", "backlog-at-risk", "backlog", 2)
    assert entry["user_id"] == ADMIN_USER and entry["institute_id"] == INSTITUTE and entry["scope"]["semester"] == 1
    assert "Chitra" not in repr(entry)                                   # the log holds counts, never names


def test_an_export_is_logged_as_an_export():
    db = sample()
    call("reports_backlog_at_risk_export", db)
    assert [(e["action"], e["rows"]) for e in db[rb.ACCESS_LOG].docs] == [("export", 3)]


def test_if_the_log_cannot_be_written_no_names_are_returned():
    db = sample()

    async def broken(doc):
        raise RuntimeError("log database is down")
    db[rb.ACCESS_LOG].insert_one = broken
    for name in ("reports_backlog_at_risk", "reports_backlog_at_risk_export"):
        with pytest.raises(HTTPException) as error:
            call(name, db)
        assert error.value.status_code == 503 and "Chitra" not in str(error.value.detail)


def test_a_refused_request_leaves_no_log_entry():
    db = sample()
    with pytest.raises(HTTPException):
        call("reports_backlog_at_risk", db, segment="everyone")
    with pytest.raises(HTTPException):
        call("reports_backlog_at_risk", db, role=3)
    assert db[rb.ACCESS_LOG].docs == []


@pytest.mark.parametrize("name", ["reports_backlog_at_risk", "reports_backlog_at_risk_export"])
@pytest.mark.parametrize("role", [1, 3, 4, 5, 6, 7, None])
def test_only_an_institute_admin_may_use_the_routes(name, role):
    with pytest.raises(HTTPException) as error:
        call(name, database(), role=role)
    assert error.value.status_code == 403


def test_a_school_institute_is_refused_and_not_logged():
    db = database(institute_type="school")
    with pytest.raises(HTTPException) as error:
        call("reports_backlog_at_risk", db)
    assert error.value.status_code == 403 and db[rb.ACCESS_LOG].docs == []


# ---- long lists and the Excel export --------------------------------------------------------------------------------------

def test_the_screen_list_is_cut_but_the_export_carries_every_row():
    many = [rec(f"EN{i:04d}", 1, [("M", "U", 10)], 6.0) for i in range(rb.LIST_LIMIT + 5)]
    db = database(many)
    shown = call("reports_backlog_at_risk", db)["students"]
    assert len(shown["items"]) == rb.LIST_LIMIT and shown["total"] == rb.LIST_LIMIT + 5 and shown["truncated"] is True
    response = call("reports_backlog_at_risk_export", db)
    book = load_workbook(io.BytesIO(response.body))
    assert book["Students"].max_row == rb.LIST_LIMIT + 5 + 1


def test_the_export_says_it_contains_names_and_keeps_formula_like_names_as_text():
    db = database([rec("EN1", 1, [("M", "U", 10)], 6.0)], studentDetails=[student("EN1", "=HYPERLINK(\"http://x\")")])
    db.c["users"].docs[0]["fullName"] = "Demo University"
    book = load_workbook(io.BytesIO(call("reports_backlog_at_risk_export", db).body))
    assert book.sheetnames == ["Students", "Not cleared", "Report info"]
    name_cell = book["Students"]["A2"]
    assert name_cell.data_type == "s" and name_cell.value.startswith("=")
    info = {row[0]: row[1] for row in book["Report info"].iter_rows(values_only=True)}
    assert info["Institute"] == "Demo University" and info["Generated by"] == "Demo University"
    assert info["CONTAINS STUDENT NAMES"].startswith("Yes") and "access log" in info["Access"]


def test_the_export_follows_the_chosen_word_for_a_backlog():
    db = database([rec("EN1", 1, [("M", "U", 10)], 6.0)], studentDetails=[student("EN1", "Asha")])
    db.c["instituteDetails"].docs[0]["reports_backlog_label"] = "Arrear"
    book = load_workbook(io.BytesIO(call("reports_backlog_at_risk_export", db).body))
    assert book["Students"]["H1"].value == "Arrear" and "Arrear" in book["Report info"]["A10"].value


# ---- what the institute calls a backlog ---------------------------------------------------------------------------------

def test_the_admin_can_choose_what_a_backlog_is_called_and_it_shows_everywhere():
    db = database([rec("EN1", 1, [("M", "U", 10)], 6.0)])
    assert report(db)["labels"]["backlog"] == "Backlog"
    out = run(router.reports_settings(router.ReportSettings(backlog_label="Arrear"),
                                      identity={"role": 2, "user_id": str(ADMIN_USER)}, db=db))
    assert out == {"backlog_label": "Arrear"}
    assert report(db)["labels"]["backlog"] == "Arrear"
    assert run(ro.build_overview(db, INSTITUTE))["labels"]["backlog"] == "Arrear"


def test_an_unknown_label_is_refused_and_only_an_admin_may_change_it():
    db = database()
    with pytest.raises(HTTPException) as error:
        run(router.reports_settings(router.ReportSettings(backlog_label="<script>"),
                                    identity={"role": 2, "user_id": str(ADMIN_USER)}, db=db))
    assert error.value.status_code == 400
    with pytest.raises(HTTPException) as error:
        run(router.reports_settings(router.ReportSettings(backlog_label="Arrear"),
                                    identity={"role": 3, "user_id": str(ADMIN_USER)}, db=db))
    assert error.value.status_code == 403
    assert "reports_backlog_label" not in db.c["instituteDetails"].docs[0]
