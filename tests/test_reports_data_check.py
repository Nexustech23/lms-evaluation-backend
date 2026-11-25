"""Data Check report: which courses, exams and results are incomplete. A fake database answers; no real database, no
network, no AI. Things that must hold: the counts equal the overview's, only this institute's data, students appear only
as counts, and nothing is written."""
import asyncio

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.api.routers import reports_overview as router
from app.services import reports_data_check as rdc
from app.services import reports_overview as ro
from tests.test_reports_overview import (
    ADMIN_USER, BATCH, INSTITUTE, OLD_BATCH, OTHER_INSTITUTE, PROGRAMME, _course, database, record,
)


def run(coro):
    return asyncio.run(coro)


def check(db, **scope):
    return run(rdc.build_data_check(db, INSTITUTE, **scope))


def exam(subject, **extra):
    base = {"_id": ObjectId(), "subject_id": subject["_id"], "semester": 1, "covered_cos": ["CO1"],
            "exam_title": "Mid Sem", "exam_type": "Mid", "faculty_id": ObjectId()}
    base.update(extra)
    return base


def test_courses_without_a_co_po_mapping_are_listed_with_their_names_and_sorted():
    mapped = _course({"CO1": {"PO1": 3}}, subject_name="Maths", subject_code="M1", semester=1)
    b = _course({}, subject_name="Zoology", subject_code="Z1", semester=2, batch_id=BATCH)
    a = _course({}, subject_name="Algebra", subject_code="A1", semester=1)
    out = check(database(subjectDetails=[mapped, b, a]))
    assert out["summary"]["courses"] == {"count": 2, "of": 3}
    assert [c["name"] for c in out["courses"]["items"]] == ["Algebra", "Zoology"]
    assert out["courses"]["items"][1]["batch"] == "CSE 2023" and out["courses"]["items"][0]["programme"] == "B.Tech CSE"


def test_exams_without_covered_cos_name_the_subject_and_the_faculty_and_skip_archived_ones():
    course = _course({"CO1": {"PO1": 3}}, subject_name="Maths")
    faculty = ObjectId()
    untagged = exam(course, covered_cos=[], faculty_id=faculty)
    out = check(database(subjectDetails=[course], newsavedDocs=[untagged, exam(course),
                                                                exam(course, covered_cos=[], is_archived=True)],
                         users=[{"_id": ADMIN_USER}, {"_id": faculty, "fullName": "Dr Rao"}]))
    assert out["summary"]["exams"] == {"count": 1, "of": 2}
    row = out["exams"]["items"][0]
    assert (row["name"], row["subject"], row["faculty"]) == ("Mid Sem", "Maths", "Dr Rao")


def test_pending_review_answer_sheets_are_counted_per_exam_most_pending_first():
    course = _course({"CO1": {"PO1": 3}}, subject_name="Maths")
    small, big = exam(course, exam_title="Quiz"), exam(course, exam_title="End Sem")
    answers = ([{"_id": ObjectId(), "exam_id": small["_id"], "evaluation_status": "pending_review"}]
               + [{"_id": ObjectId(), "exam_id": big["_id"], "evaluation_status": "pending_review"} for _ in range(3)]
               + [{"_id": ObjectId(), "exam_id": big["_id"], "evaluation_status": "evaluated"}])
    out = check(database(subjectDetails=[course], newsavedDocs=[small, big], answerDetails=answers))
    assert out["summary"]["evaluation"] == {"pending": 4, "of": 5}
    assert [(r["name"], r["pending"], r["total"]) for r in out["evaluation"]["items"]] == [
        ("End Sem", 3, 4), ("Quiz", 1, 1)]


def test_results_needing_regeneration_are_counted_per_batch_and_semester_without_student_ids():
    out = check(database([record("S1", 2, ("A",)), record("EN-77", 1, ("U",), stale=True),
                          record("EN-78", 1, ("A",), stale=True), record("EN-79", 2, ("A",), stale=True, batch=OLD_BATCH)]))
    assert out["summary"]["stale"]["count"] == 3
    assert [(r["batch"], r["semester"], r["students"]) for r in out["stale"]["items"]] == [
        ("CSE 2022", 2, 1), ("CSE 2023", 1, 2)]
    assert "EN-77" not in repr(out) and "student_id" not in repr(out)


def test_the_counts_always_equal_the_overview_tiles():
    course_a, course_b = _course({"CO1": {"PO1": 3}}), _course({})
    exams = [exam(course_a, covered_cos=[]), exam(course_b)]
    answers = [{"_id": ObjectId(), "exam_id": exams[0]["_id"], "evaluation_status": "pending_review"}]
    db = database([record("S1", 1, ("A",)), record("S2", 1, ("A",), stale=True)],
                  subjectDetails=[course_a, course_b], newsavedDocs=exams, answerDetails=answers)
    report = check(db)
    tiles = run(ro.build_overview(db, INSTITUTE))["tiles"]
    assert report["summary"]["problems"] == tiles["data_checks"]["value"] == 3
    assert report["summary"]["evaluation"]["pending"] == tiles["evaluation"]["pending"] == 1
    assert report["summary"]["evaluation"]["of"] == tiles["evaluation"]["of"]


def test_all_clear_only_when_the_checks_ran_and_found_nothing():
    course = _course({"CO1": {"PO1": 3}})
    clear = check(database(subjectDetails=[course], newsavedDocs=[exam(course)]))
    assert clear["all_clear"] is True and clear["summary"]["problems"] == 0
    empty = check(database())
    assert empty["all_clear"] is False and empty["checks_ran"] is False and empty["summary"]["problems"] is None
    pending = check(database(subjectDetails=[course], newsavedDocs=[(e := exam(course))],
                             answerDetails=[{"_id": ObjectId(), "exam_id": e["_id"], "evaluation_status": "pending_review"}]))
    assert pending["all_clear"] is False


def test_long_lists_are_cut_but_the_count_covers_every_row():
    courses = [_course({}, subject_name=f"C{i:03d}") for i in range(rdc.LIST_LIMIT + 5)]
    out = check(database(subjectDetails=courses))
    assert len(out["courses"]["items"]) == rdc.LIST_LIMIT
    assert out["courses"]["total"] == rdc.LIST_LIMIT + 5 and out["courses"]["truncated"] is True
    assert out["summary"]["courses"]["count"] == rdc.LIST_LIMIT + 5


def test_scope_narrows_the_courses_and_returns_the_filters():
    here = _course({}, subject_name="Here")
    there = _course({}, subject_name="There", programme_id=ObjectId())
    out = check(database(subjectDetails=[here, there]), programme=str(PROGRAMME))
    assert [c["name"] for c in out["courses"]["items"]] == ["Here"]
    assert out["scope"]["programme"] == str(PROGRAMME) and out["filters"]["semesters"]


def test_another_institutes_courses_are_never_listed_and_nothing_is_written():
    mine = _course({}, subject_name="Mine")
    theirs = _course({}, subject_name="Theirs", institute=OTHER_INSTITUTE)
    db = database(subjectDetails=[mine, theirs])
    out = check(db)
    assert [c["name"] for c in out["courses"]["items"]] == ["Mine"]
    assert all(col.updates == 0 for col in db.c.values())


def test_a_school_institute_is_refused():
    with pytest.raises(HTTPException) as error:
        check(database(institute_type="school"))
    assert error.value.status_code == 403


@pytest.mark.parametrize("role", [1, 3, 4, 5, 6, 7, None])
def test_only_an_institute_admin_may_use_the_route(role):
    with pytest.raises(HTTPException) as error:
        run(router.reports_data_check(programme=None, batch=None, semester=None,
                                      identity={"role": role, "user_id": str(ADMIN_USER)}, db=database()))
    assert error.value.status_code == 403


def test_the_route_returns_the_admins_own_report():
    out = run(router.reports_data_check(programme=None, batch=None, semester=None,
                                        identity={"role": 2, "user_id": str(ADMIN_USER)},
                                        db=database(subjectDetails=[_course({}, subject_name="Mine")])))
    assert out["summary"]["courses"]["count"] == 1
