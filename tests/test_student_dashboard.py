"""Student dashboard: the real values for a student's own dashboard. A fake database answers; no real database, no
network, no AI. Things that must hold: only the student's own data, no invented numbers, nothing is written."""
import asyncio

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.api.routers import student_dashboard as router
from app.services import student_dashboard as sd
from tests.test_bulk_update_apply import Db

INSTITUTE, OTHER_INSTITUTE = ObjectId(), ObjectId()
BATCH, OTHER_BATCH = ObjectId(), ObjectId()
USER = ObjectId()


def run(coro):
    return asyncio.run(coro)


def student(user=USER, enrollment="EN100", roll="7", institute=INSTITUTE, **extra):
    base = {"_id": ObjectId(), "user_id": user, "institute_id": institute, "role": 4, "name": "Asha Rao",
            "enrollment_no": enrollment, "roll_no": roll, "is_deleted": False}
    base.update(extra)
    return base


def relation(stu, subject, semester=1, batch=BATCH, **extra):
    base = {"_id": ObjectId(), "student_id": stu["_id"], "user_id": stu["user_id"], "subject_id": subject["_id"],
            "semester": semester, "batch_id": batch, "is_deleted": False}
    base.update(extra)
    return base


def subject(deleted=False):
    return {"_id": ObjectId(), "is_deleted": deleted}


def record(student_id="EN100", semester=1, cgpa=8.0, batch=BATCH, institute=INSTITUTE, stale=False, subjects=None):
    return {"institute_id": institute, "student_id": student_id, "batch_id": batch, "semester_no": semester,
            "term_label": f"Sem {semester}", "tgpa": cgpa, "cgpa": cgpa, "is_stale": stale,
            "subjects": subjects if subjects is not None else [{"subject": "Maths", "credits": 4, "marks": 81, "grade": "A"}]}


def database(stu, relations=(), subjects=(), records=(), batches=None):
    return Db(studentDetails=[stu], StudentSubjectRelationModel=list(relations), subjectDetails=list(subjects),
              batchDetails=batches if batches is not None else [{"_id": BATCH, "institute_id": INSTITUTE, "total_semesters": 6}],
              academic_transcripts=list(records))


def dashboard(db, user=USER):
    return run(sd.build_dashboard(db, str(user)))


# ---- real values ---------------------------------------------------------------------------------------------------

def test_the_students_own_values_are_returned():
    stu = student()
    s1, s2 = subject(), subject()
    db = database(stu, [relation(stu, s1, 1), relation(stu, s2, 2)], [s1, s2],
                  [record(semester=1, cgpa=7.5), record(semester=2, cgpa=8.25,
                                                       subjects=[{"subject": "Physics", "credits": 3, "marks": 74.5, "grade": "B"}])])
    out = dashboard(db)
    assert out["name"] == "Asha Rao" and out["subjects_count"] == 2
    assert out["semesters"] == [1, 2] and out["total_semesters"] == 6
    assert out["cgpa"] == 8.25 and out["latest_semester"] == 2           # the latest semester's cumulative CGPA
    assert out["scores"] == [{"subject": "Physics", "marks": 74.5, "grade": "B", "credits": 3.0}]
    assert out["results_stale"] is False


def test_a_deleted_subject_is_not_counted():
    stu = student()
    live, gone = subject(), subject(deleted=True)
    db = database(stu, [relation(stu, live), relation(stu, gone)], [live, gone])
    assert dashboard(db)["subjects_count"] == 1


def test_a_student_with_nothing_yet_gets_empty_values_not_invented_ones():
    out = dashboard(database(student()))
    assert out["subjects_count"] == 0 and out["semesters"] == [] and out["total_semesters"] is None
    assert out["cgpa"] is None and out["scores"] == [] and out["latest_semester"] is None


def test_what_the_app_does_not_store_is_left_empty():
    out = dashboard(database(student()))
    assert out["attendance"] is None and out["assignments"] is None
    assert out["timetable"] == [] and out["notifications"] == []


# ---- only the student's own data -----------------------------------------------------------------------------------

def test_another_students_results_and_subjects_are_never_returned():
    me, other = student(enrollment="EN100", roll="7"), student(user=ObjectId(), enrollment="EN200", roll="8")
    mine, theirs = subject(), subject()
    db = Db(studentDetails=[me, other], StudentSubjectRelationModel=[relation(me, mine), relation(other, theirs), relation(other, subject())],
            subjectDetails=[mine, theirs], batchDetails=[{"_id": BATCH, "institute_id": INSTITUTE, "total_semesters": 6}],
            academic_transcripts=[record("EN100", cgpa=7.0), record("EN200", cgpa=9.9)])
    out = dashboard(db)
    assert out["subjects_count"] == 1 and out["cgpa"] == 7.0


def test_another_colleges_results_with_the_same_id_are_never_returned():
    stu = student()
    db = database(stu, records=[record("EN100", cgpa=9.9, institute=OTHER_INSTITUTE)])
    assert dashboard(db)["cgpa"] is None


def test_a_roll_number_counts_only_for_a_batch_the_student_is_in():
    stu = student(enrollment="EN100", roll="7")
    s = subject()
    own = record("7", cgpa=8.0, batch=BATCH)
    elsewhere = record("7", semester=2, cgpa=9.9, batch=OTHER_BATCH)       # somebody else's roll number 7
    out = dashboard(database(stu, [relation(stu, s)], [s], [own, elsewhere]))
    assert out["cgpa"] == 8.0 and out["latest_semester"] == 1


def test_a_roll_number_record_is_ignored_when_the_student_has_no_batch():
    stu = student(enrollment="EN100", roll="7")
    assert dashboard(database(stu, records=[record("7", cgpa=9.9)]))["cgpa"] is None


def test_a_student_without_ids_matches_no_results():
    stu = student(enrollment="", roll=None)
    assert dashboard(database(stu, records=[record("", cgpa=9.9)]))["cgpa"] is None


# ---- results that need regenerating ------------------------------------------------------------------------------------

def test_stale_results_are_not_shown_and_are_flagged():
    stu = student()
    out = dashboard(database(stu, records=[record(semester=1, cgpa=7.0), record(semester=2, cgpa=9.0, stale=True)]))
    assert out["cgpa"] == 7.0 and out["latest_semester"] == 1 and out["results_stale"] is True


def test_when_every_record_is_stale_no_cgpa_is_shown():
    stu = student()
    out = dashboard(database(stu, records=[record(cgpa=9.0, stale=True)]))
    assert out["cgpa"] is None and out["scores"] == [] and out["results_stale"] is True


def test_a_marks_value_that_is_not_a_number_becomes_empty():
    stu = student()
    rec = record(subjects=[{"subject": "Maths", "credits": 4, "marks": "AB", "grade": "F"}, {"subject": "Art", "marks": True}])
    scores = dashboard(database(stu, records=[rec]))["scores"]
    assert [s["marks"] for s in scores] == [None, None]


# ---- who may ask, and that nothing is written ------------------------------------------------------------------------

@pytest.mark.parametrize("user", [ObjectId(), "not-an-id"])
def test_an_unknown_user_gets_not_found(user):
    with pytest.raises(HTTPException) as error:
        run(sd.build_dashboard(database(student()), str(user)))
    assert error.value.status_code == 404


def test_a_deleted_student_gets_not_found():
    with pytest.raises(HTTPException) as error:
        dashboard(database(student(is_deleted=True)))
    assert error.value.status_code == 404


@pytest.mark.parametrize("role", [1, 2, 3, 5, 6, 7, None])
def test_only_an_institute_student_may_use_the_route(role):
    with pytest.raises(HTTPException) as error:
        run(router.student_dashboard(identity={"role": role, "user_id": str(USER)}, db=database(student())))
    assert error.value.status_code == 403


def test_the_route_returns_the_logged_in_students_values_and_writes_nothing():
    stu = student()
    s = subject()
    db = database(stu, [relation(stu, s)], [s], [record(cgpa=8.5)])
    out = run(router.student_dashboard(identity={"role": 4, "user_id": str(USER)}, db=db))
    assert out["cgpa"] == 8.5 and out["subjects_count"] == 1
    assert all(col.updates == 0 for col in db.c.values())
