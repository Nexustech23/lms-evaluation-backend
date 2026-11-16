"""The subjects a student can enrol in: for one semester (a college), or the whole class (a school). No database."""
import pytest
from bson import ObjectId

from app.api.routers.student_subjects import subject_query

BATCH = str(ObjectId())
DEPARTMENT = str(ObjectId())


def test_a_college_asks_for_one_semester():
    query = subject_query(BATCH, "3", None)
    assert query == {"batch_id": ObjectId(BATCH), "is_deleted": {"$ne": True}, "semester": 3}


@pytest.mark.parametrize("semester", [None, ""])
def test_a_school_asks_without_a_semester_and_gets_the_whole_class(semester):
    query = subject_query(BATCH, semester, None)
    assert query == {"batch_id": ObjectId(BATCH), "is_deleted": {"$ne": True}}
    assert "semester" not in query


def test_a_department_narrows_it_when_it_is_valid():
    assert subject_query(BATCH, "1", DEPARTMENT)["department_id"] == ObjectId(DEPARTMENT)
    assert "department_id" not in subject_query(BATCH, "1", "not-an-id")
    assert "department_id" not in subject_query(BATCH, "1", "")
    assert "department_id" not in subject_query(BATCH, None, None)


def test_a_semester_zero_is_still_a_semester():
    assert subject_query(BATCH, "0", None)["semester"] == 0


def test_a_bad_semester_is_still_rejected_not_ignored():
    with pytest.raises(ValueError):
        subject_query(BATCH, "abc", None)


def test_the_endpoint_takes_the_semester_as_optional():
    import inspect
    from app.api.routers.student_subjects import get_subjects_by_filters
    default = inspect.signature(get_subjects_by_filters).parameters["semester"].default
    assert default.default is None            # Query(None): not required
