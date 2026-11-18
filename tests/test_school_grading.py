"""School grading: a student's own percentage falls in an editable band (CBSE nine-point scale by default). A college is
not touched. A fake database answers; no AI call, no real database, no network."""
import asyncio
from datetime import datetime

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.api.routers import school_grading as router
from app.schemas.school_grading import SchoolGradeBand, SchoolGradingRequest
from app.utils import school_grading as sg


def run(coro):
    return asyncio.run(coro)


# ---- a tiny fake database ----------------------------------------------------------------------------------------------

class Cursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, key, direction=1):
        self.docs.sort(key=lambda d: d.get(key), reverse=direction < 0)
        return self

    def __aiter__(self):
        async def gen():
            for doc in self.docs:
                yield doc
        return gen()


class Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    @staticmethod
    def _match(doc, query):
        return all(doc.get(k) == v for k, v in query.items() if not isinstance(v, dict))

    async def find_one(self, query, projection=None):
        return next((d for d in self.docs if self._match(d, query)), None)

    def find(self, query=None, projection=None):
        return Cursor(d for d in self.docs if self._match(d, query or {}))

    async def update_one(self, query, update, upsert=False):
        doc = await self.find_one(query)
        if doc is None and upsert:
            doc = {**query, **update.get("$setOnInsert", {})}
            self.docs.append(doc)
        if doc is not None:
            doc.update(update.get("$set", {}))

    async def delete_one(self, query):
        doc = await self.find_one(query)
        if doc is not None:
            self.docs.remove(doc)


class Db:
    def __init__(self, **collections):
        self.collections = {name: Collection(docs) for name, docs in collections.items()}

    def __getitem__(self, name):
        return self.collections.setdefault(name, Collection())


SCHOOL, COLLEGE = ObjectId(), ObjectId()


def institutes():
    return [{"_id": SCHOOL, "institute_type": "school"}, {"_id": COLLEGE, "institute_type": "college"}]


# ---- the default CBSE bands --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("percentage,grade,point", [
    (100, "A1", 10), (91, "A1", 10), (90.99, "A2", 9), (81, "A2", 9), (80, "B1", 8), (71, "B1", 8), (70.5, "B2", 7),
    (61, "B2", 7), (60, "C1", 6), (51, "C1", 6), (50, "C2", 5), (41, "C2", 5), (40.99, "D", 4), (33, "D", 4),
    (32.99, "E1", 0), (21, "E1", 0), (20.99, "E2", 0), (0, "E2", 0), (-5, "E2", 0),
])
def test_the_default_bands_are_the_cbse_nine_point_scale(percentage, grade, point):
    band = sg.grade_for(percentage, sg.default_config())
    assert (band["grade"], band["grade_point"]) == (grade, point)


def test_no_percentage_has_no_grade():
    config = sg.default_config()
    assert sg.grade_for(None, config) is None and sg.grade_for("abc", config) is None and sg.grade_for(float("nan"), config) is None


def test_the_default_pass_mark_is_33():
    config = sg.default_config()
    assert config["pass_percentage"] == 33
    assert sg.is_pass(33, config) and not sg.is_pass(32.9, config) and not sg.is_pass(None, config)


def test_the_default_is_a_copy_so_an_edit_never_changes_it():
    config = sg.default_config()
    config["bands"][0]["grade"] = "X"
    assert sg.default_config()["bands"][0]["grade"] == "A1"


def test_the_default_setup_passes_its_own_validation():
    config, error = sg.validate_config(sg.default_config())
    assert error is None and config == sg.default_config()


# ---- validation --------------------------------------------------------------------------------------------------------

def band(grade, minimum, point=5):
    return {"grade": grade, "min_percentage": minimum, "grade_point": point}


def check(bands, **extra):
    return sg.validate_config({"pass_percentage": 33, "bands": bands, **extra})


def test_a_custom_scale_is_accepted_and_sorted_highest_first():
    config, error = check([band("C", 0), band("A", 80, 10), band("B", 50, 7)])
    assert error is None
    assert [b["grade"] for b in config["bands"]] == ["A", "B", "C"]
    assert sg.grade_for(79.9, config)["grade"] == "B" and sg.grade_for(80, config)["grade"] == "A"


@pytest.mark.parametrize("bands,message", [
    ([], "at least one grade"),
    ([band("A", 50)], "must start at 0"),
    ([band("A", 90), band("A", 0)], "listed twice"),
    ([band("A", 90), band("a", 0)], "listed twice"),
    ([band("A", 50), band("B", 50), band("C", 0)], "same marks"),
    ([band("A", 101), band("B", 0)], "between 0 and 100"),
    ([band("A", -1), band("B", 0)], "between 0 and 100"),
    ([band("A", 90, 11), band("B", 0)], "between 0 and 10"),
    ([band("A" * 9, 90), band("B", 0)], "longer than"),
    ([band("", 90), band("B", 0)], "needs a name"),
    ([band("A", "high"), band("B", 0)], "must be a number"),
    ([band("A", None), band("B", 0)], "must be a number"),
    ([band("A", True), band("B", 0)], "must be a number"),
    ([band("A", float("inf")), band("B", 0)], "must be a number"),
    (["A"], "not valid"),
])
def test_a_bad_scale_is_refused_with_a_reason(bands, message):
    config, error = check(bands)
    assert config is None and message in error


def test_the_pass_mark_must_be_a_percentage():
    assert check([band("A", 0)], pass_percentage=150)[1] is not None
    assert check([band("A", 0)], pass_percentage="x")[1] is not None
    assert check([band("A", 0)], pass_percentage=0)[1] is None


def test_too_many_grades_are_refused():
    bands = [band(f"G{i}", 21 - i) for i in range(21)]
    assert "at most" in sg.validate_config({"bands": bands})[1]


def test_something_that_is_not_an_object_is_refused():
    assert sg.validate_config(None)[0] is None and sg.validate_config([])[0] is None


# ---- saved setups ------------------------------------------------------------------------------------------------------

def test_no_saved_setup_means_the_default():
    assert sg.config_from_doc(None) == sg.default_config()


def test_a_saved_setup_is_used():
    saved = {"pass_percentage": 40, "bands": [band("A", 60, 10), band("B", 0, 6)]}
    config = sg.config_from_doc(saved)
    assert config["pass_percentage"] == 40 and [b["grade"] for b in config["bands"]] == ["A", "B"]


def test_a_saved_setup_that_has_become_invalid_falls_back_to_the_default():
    assert sg.config_from_doc({"pass_percentage": 33, "bands": [band("A", 50)]}) == sg.default_config()


def test_assigning_school_grades_adds_only_the_grade():
    rows = [{"student_id": "a", "overall_total": 450}, {"student_id": "b", "overall_total": 150}, {"student_id": "c"}]
    sg.assign_school_grades(rows, sg.default_config(), max_marks=500)
    assert [r["grade"] for r in rows] == ["A2", "E1", "E2"]     # 90%, 30%, nothing
    assert all(set(r) <= {"student_id", "overall_total", "grade"} for r in rows)    # the export reads any other key as a subject


def test_assigning_with_no_maximum_does_not_divide_by_zero():
    rows = [{"overall_total": 10}]
    assert sg.assign_school_grades(rows, sg.default_config(), max_marks=0)[0]["grade"] == "E2"


# ---- school or college -------------------------------------------------------------------------------------------------

def test_a_school_gets_its_grading_and_a_college_gets_none():
    db = Db(instituteDetails=institutes())
    assert run(sg.config_if_school(db, SCHOOL)) == sg.default_config()
    assert run(sg.config_if_school(db, COLLEGE)) is None
    assert run(sg.config_if_school(db, ObjectId())) is None


def test_a_school_with_its_own_setup_uses_it():
    saved = {"institute_id": SCHOOL, "pass_percentage": 40, "bands": [band("Pass", 40, 5), band("Fail", 0, 0)]}
    db = Db(instituteDetails=institutes(), schoolGradings=[saved])
    config = run(sg.config_if_school(db, SCHOOL))
    assert [b["grade"] for b in config["bands"]] == ["Pass", "Fail"]


# ---- the routes --------------------------------------------------------------------------------------------------------

@pytest.fixture
def as_institute(monkeypatch):
    def pick(institute_id):
        async def fake(identity, db):
            return {"_id": "user"}, institute_id, None
        monkeypatch.setattr(router, "get_current_user_and_institute", fake)
    return pick


def call(function, db, *args):
    return run(function(*args, identity={}, db=db))


def test_a_college_cannot_use_the_school_routes(as_institute):
    as_institute(COLLEGE)
    db = Db(instituteDetails=institutes())
    payload = SchoolGradingRequest(bands=[SchoolGradeBand(grade="A", min_percentage=0, grade_point=1)])
    for function, args in [(router.get_school_grading, ()), (router.save_school_grading, (payload,)), (router.reset_school_grading, ())]:
        with pytest.raises(HTTPException) as error:
            call(function, db, *args)
        assert error.value.status_code == 400 and "school" in error.value.detail


def test_a_school_with_nothing_saved_sees_the_cbse_default(as_institute):
    as_institute(SCHOOL)
    result = call(router.get_school_grading, Db(instituteDetails=institutes()))
    assert result["data"]["is_default"] is True and result["data"]["bands"] == sg.default_config()["bands"]
    assert result["default"] == sg.default_config()


def test_saving_edits_the_scale_and_reading_it_back_shows_them(as_institute):
    as_institute(SCHOOL)
    db = Db(instituteDetails=institutes())
    bands = [SchoolGradeBand(grade="A", min_percentage=75, grade_point=10),
             SchoolGradeBand(grade="B", min_percentage=0, grade_point=6)]
    saved = call(router.save_school_grading, db, SchoolGradingRequest(pass_percentage=35, bands=bands))
    assert saved["data"]["is_default"] is False and saved["data"]["pass_percentage"] == 35
    again = call(router.get_school_grading, db)
    assert again["data"]["is_default"] is False and [b["grade"] for b in again["data"]["bands"]] == ["A", "B"]
    assert len(db["schoolGradings"].docs) == 1
    call(router.save_school_grading, db, SchoolGradingRequest(pass_percentage=35, bands=bands))
    assert len(db["schoolGradings"].docs) == 1                        # saving twice edits, it does not add


def test_a_bad_scale_is_not_saved(as_institute):
    as_institute(SCHOOL)
    db = Db(instituteDetails=institutes())
    bad = SchoolGradingRequest(bands=[SchoolGradeBand(grade="A", min_percentage=50, grade_point=10)])
    with pytest.raises(HTTPException) as error:
        call(router.save_school_grading, db, bad)
    assert error.value.status_code == 400 and "start at 0" in error.value.detail
    assert db["schoolGradings"].docs == []


def test_reset_brings_back_the_cbse_default(as_institute):
    as_institute(SCHOOL)
    db = Db(instituteDetails=institutes())
    bands = [SchoolGradeBand(grade="Only", min_percentage=0, grade_point=1)]
    call(router.save_school_grading, db, SchoolGradingRequest(bands=bands))
    result = call(router.reset_school_grading, db)
    assert result["data"]["is_default"] is True and db["schoolGradings"].docs == []
    assert call(router.get_school_grading, db)["data"]["bands"] == sg.default_config()["bands"]


# ---- results use it ----------------------------------------------------------------------------------------------------

def combined(db, institute_id):
    from app.api.routers import subject_results
    return run(subject_results._get_combined_results(db, institute_id, str(ObjectId()), "1"))


def imported(institute_id, marks):
    return [{"institute_id": institute_id, "student_name": name, "marks": value} for name, value in marks.items()]


def test_a_school_with_no_grading_saved_still_gets_cbse_grades_in_combined_results():
    batch = ObjectId()
    docs = [{**d, "batch_id": batch, "semester": 1} for d in imported(SCHOOL, {"Asha": 95, "Ravi": 45, "Meena": 20})]
    db = Db(instituteDetails=institutes(), importedMarks=docs)
    from app.api.routers import subject_results
    body, code = run(subject_results._get_combined_results(db, SCHOOL, str(batch), "1"))
    assert code == 200
    assert {row["student_id"]: row["grade"] for row in body["data"]} == {"Asha": "A1", "Ravi": "C2", "Meena": "E2"}


def test_a_school_uses_its_edited_bands_in_combined_results():
    batch = ObjectId()
    docs = [{**d, "batch_id": batch, "semester": 1} for d in imported(SCHOOL, {"Asha": 95, "Ravi": 60})]
    saved = {"institute_id": SCHOOL, "pass_percentage": 33, "bands": [band("Merit", 90, 10), band("Pass", 0, 5)]}
    db = Db(instituteDetails=institutes(), importedMarks=docs, schoolGradings=[saved])
    from app.api.routers import subject_results
    body, code = run(subject_results._get_combined_results(db, SCHOOL, str(batch), "1"))
    assert {row["student_id"]: row["grade"] for row in body["data"]} == {"Asha": "Merit", "Ravi": "Pass"}


def test_a_college_without_relative_grading_still_gets_the_same_error_as_before():
    from app.api.routers import subject_results
    body, code = run(subject_results._get_combined_results(Db(instituteDetails=institutes()), COLLEGE, str(ObjectId()), "1"))
    assert code == 404 and "Relative grading" in body["error"]


def test_a_college_still_grades_by_rank():
    batch = ObjectId()
    docs = [{**d, "batch_id": batch, "semester": 1} for d in imported(COLLEGE, {"A": 99, "B": 98, "C": 97, "D": 96})]
    relative = {"university_id": COLLEGE, "a_plus_percentage": 50, "a_percentage": 50}
    db = Db(instituteDetails=institutes(), importedMarks=docs, relativeGradings=[relative])
    from app.api.routers import subject_results
    body, code = run(subject_results._get_combined_results(db, COLLEGE, str(batch), "1"))
    assert code == 200 and [row["grade"] for row in body["data"]] == ["A+", "A+", "A", "A"]    # by position in the class, not by marks


# ---- the per-subject results use it too --------------------------------------------------------------------------------

def subject_db(institute_id, marks_by_student, extra=()):
    subject_id, exam_id = ObjectId(), ObjectId()
    answers = [{"_id": ObjectId(), "exam_id": exam_id, "student_id": name, "filename": f"{name}.pdf",
                "total_final_marks": marks, "total_max_marks": 100} for name, marks in marks_by_student.items()]
    db = Db(
        instituteDetails=institutes(),
        subjectDetails=[{"_id": subject_id, "institute_id": institute_id, "subject_name": "Maths", "is_deleted": False}],
        newsavedDocs=[{"_id": exam_id, "subject_id": subject_id, "exam_title": "Annual", "weightage": 100,
                       "exam_date": datetime(2026, 3, 1)}],
        answerDetails=answers,
        **dict(extra),
    )
    return db, str(subject_id)


def test_a_school_subject_result_carries_cbse_grades_and_points():
    from app.api.routers import subject_results
    db, subject_id = subject_db(SCHOOL, {"Asha": 95, "Ravi": 45, "Meena": 20})
    body, code = run(subject_results._get_subject_results(db, subject_id, SCHOOL))
    assert code == 200
    by_student = {s["student_id"]: (s.get("course_grade"), s.get("grade_point")) for s in body["students"]}
    assert by_student == {"Asha": ("A1", 10.0), "Ravi": ("C2", 5.0), "Meena": ("E2", 0.0)}


def test_a_college_subject_result_without_relative_grading_has_no_grade():
    from app.api.routers import subject_results
    db, subject_id = subject_db(COLLEGE, {"Asha": 95})
    body, code = run(subject_results._get_subject_results(db, subject_id, COLLEGE))
    assert code == 200 and "course_grade" not in body["students"][0]
