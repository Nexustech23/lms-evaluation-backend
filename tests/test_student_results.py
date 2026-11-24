"""Student results: the logged-in student's own semester results. A fake database answers; no real database, no
network, no AI. Things that must hold: only the student's own results, stale results hidden, nothing is written."""
import asyncio

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.api.routers import student_results as router
from app.services import student_results as sr
from tests.test_bulk_update_apply import Db
from tests.test_student_dashboard import (
    BATCH, INSTITUTE, OTHER_BATCH, OTHER_INSTITUTE, USER, database, record, relation, student, subject,
)


def run(coro):
    return asyncio.run(coro)


def results(db, user=USER):
    return run(sr.build_results(db, str(user)))


def full_record(semester=1, cgpa=8.0, student_id="EN100", batch=BATCH, institute=INSTITUTE, is_stale=False):
    rec = record(student_id, semester=semester, cgpa=cgpa, batch=batch, institute=institute, stale=is_stale, subjects=[
        {"subject": "Maths", "credits": 4, "marks": 81, "grade": "A", "gradePoint": 9, "creditPoints": 36},
        {"subject": "Physics", "credits": 3, "marks": 64.5, "grade": "B", "gradePoint": 7, "creditPoints": 21},
    ])
    rec.update({"overall_total": 145.5, "total_credits": 7, "total_credit_points": 57, "tgpa": 8.14})
    return rec


# ---- the student's own results ------------------------------------------------------------------------------------

def test_every_usable_semester_is_returned_in_order_with_its_subjects():
    stu = student()
    out = results(database(stu, records=[full_record(2, 8.4), full_record(1, 7.9)]))
    assert [s["semester"] for s in out["semesters"]] == [1, 2]
    first = out["semesters"][0]
    assert first["label"] == "Sem 1" and first["tgpa"] == 8.14 and first["overall_total"] == 145.5
    assert first["total_credits"] == 7.0 and first["total_credit_points"] == 57.0
    assert first["subjects"][0] == {"subject": "Maths", "credits": 4.0, "marks": 81.0, "grade": "A",
                                    "grade_point": 9.0, "credit_points": 36.0}
    assert out["cgpa"] == 8.4                                   # the latest semester's cumulative CGPA
    assert out["name"] == "Asha Rao" and out["enrollment_no"] == "EN100" and out["results_stale"] is False


def test_a_student_with_no_results_gets_an_empty_list_not_invented_values():
    out = results(database(student()))
    assert out["semesters"] == [] and out["cgpa"] is None and out["results_stale"] is False


# ---- only the student's own data -----------------------------------------------------------------------------------

def test_another_students_results_are_never_returned():
    me, other = student(enrollment="EN100", roll="7"), student(user=ObjectId(), enrollment="EN200", roll="8")
    db = Db(studentDetails=[me, other], StudentSubjectRelationModel=[], subjectDetails=[], batchDetails=[],
            academic_transcripts=[full_record(1, 7.0), full_record(1, 9.9, student_id="EN200")])
    out = results(db)
    assert len(out["semesters"]) == 1 and out["cgpa"] == 7.0


def test_another_colleges_results_with_the_same_id_are_never_returned():
    stu = student()
    assert results(database(stu, records=[full_record(institute=OTHER_INSTITUTE)]))["semesters"] == []


def test_a_roll_number_counts_only_for_a_batch_the_student_is_in():
    stu = student(enrollment="EN100", roll="7")
    s = subject()
    own = full_record(1, 8.0, student_id="7", batch=BATCH)
    elsewhere = full_record(2, 9.9, student_id="7", batch=OTHER_BATCH)
    out = results(database(stu, [relation(stu, s)], [s], [own, elsewhere]))
    assert [x["semester"] for x in out["semesters"]] == [1] and out["cgpa"] == 8.0


# ---- results that need regenerating ------------------------------------------------------------------------------------

def test_stale_semesters_are_hidden_and_flagged():
    out = results(database(student(), records=[full_record(1, 7.0), full_record(2, 9.0, is_stale=True)]))
    assert [s["semester"] for s in out["semesters"]] == [1] and out["cgpa"] == 7.0 and out["results_stale"] is True


def test_when_every_semester_is_stale_nothing_is_shown():
    out = results(database(student(), records=[full_record(1, 9.0, is_stale=True)]))
    assert out["semesters"] == [] and out["cgpa"] is None and out["results_stale"] is True


def test_values_that_are_not_numbers_become_empty_and_odd_subject_rows_are_skipped():
    rec = full_record(1, 8.0)
    rec["subjects"] = [{"subject": "Maths", "credits": "x", "marks": "AB", "grade": "F", "creditPoints": True}, "junk", None]
    sub = results(database(student(), records=[rec]))["semesters"][0]["subjects"]
    assert sub == [{"subject": "Maths", "credits": None, "marks": None, "grade": "F", "grade_point": None, "credit_points": None}]


# ---- who may ask, and that nothing is written ------------------------------------------------------------------------

@pytest.mark.parametrize("user", [ObjectId(), "not-an-id"])
def test_an_unknown_user_gets_not_found(user):
    with pytest.raises(HTTPException) as error:
        run(sr.build_results(database(student()), str(user)))
    assert error.value.status_code == 404


def test_a_deleted_student_gets_not_found():
    with pytest.raises(HTTPException) as error:
        results(database(student(is_deleted=True)))
    assert error.value.status_code == 404


@pytest.mark.parametrize("role", [1, 2, 3, 5, 6, 7, None])
def test_only_an_institute_student_may_use_the_route(role):
    with pytest.raises(HTTPException) as error:
        run(router.student_results(identity={"role": role, "user_id": str(USER)}, db=database(student())))
    assert error.value.status_code == 403


def test_the_route_returns_the_logged_in_students_results_and_writes_nothing():
    db = database(student(), records=[full_record(1, 8.5)])
    out = run(router.student_results(identity={"role": 4, "user_id": str(USER)}, db=db))
    assert out["cgpa"] == 8.5 and len(out["semesters"]) == 1
    assert all(col.updates == 0 for col in db.c.values())
