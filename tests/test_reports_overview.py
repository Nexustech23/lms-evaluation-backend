"""Reports overview: the numbers for the Institute Admin's Reports dashboard. A fake database answers; no real
database, no network, no AI. Things that must hold: only the admin's own institute, college only, a number that cannot
be worked out is None (never 0), results needing regeneration are left out, and nothing is written."""
import asyncio
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.api.routers import reports_overview as router
from app.services import reports_overview as ro
from tests.test_bulk_update_apply import Db

INSTITUTE, OTHER_INSTITUTE, ADMIN_USER = ObjectId(), ObjectId(), ObjectId()
PROGRAMME, OTHER_PROGRAMME = ObjectId(), ObjectId()
BATCH, OLD_BATCH, OTHER_PROGRAMME_BATCH = ObjectId(), ObjectId(), ObjectId()


def run(coro):
    return asyncio.run(coro)


def overview(db, **scope):
    return run(ro.build_overview(db, INSTITUTE, **scope))


def record(student="S1", semester=1, grades=("A", "B"), cgpa=8.0, batch=BATCH, institute=INSTITUTE, stale=False,
           **extra):
    base = {"_id": ObjectId(), "student_id": student, "institute_id": institute, "batch_id": batch,
            "semester_no": semester, "cgpa": cgpa, "is_stale": stale,
            "subjects": [{"subject": f"Sub{i}", "grade": g} for i, g in enumerate(grades)]}
    base.update(extra)
    return base


def database(records=(), institute_type="college", **extra):
    collections = dict(
        instituteDetails=[{"_id": INSTITUTE, "user_id": ADMIN_USER, "institute_type": institute_type}],
        users=[{"_id": ADMIN_USER}],
        programmeDetails=[{"_id": PROGRAMME, "institute_id": INSTITUTE, "programme_name": "B.Tech CSE"},
                          {"_id": OTHER_PROGRAMME, "institute_id": INSTITUTE, "programme_name": "MBA"}],
        batchDetails=[
            {"_id": BATCH, "institute_id": INSTITUTE, "programme_id": PROGRAMME, "batch_name": "CSE 2023",
             "total_semesters": 8, "created_at": datetime(2023, 7, 1)},
            {"_id": OLD_BATCH, "institute_id": INSTITUTE, "programme_id": PROGRAMME, "batch_name": "CSE 2022",
             "total_semesters": 8, "created_at": datetime(2022, 7, 1)},
            {"_id": OTHER_PROGRAMME_BATCH, "institute_id": INSTITUTE, "programme_id": OTHER_PROGRAMME,
             "batch_name": "MBA 2023", "total_semesters": 4, "created_at": datetime(2023, 7, 1)}],
        academic_transcripts=list(records), subjectDetails=[], newsavedDocs=[], answerDetails=[],
        studentDetails=[], StudentSubjectRelationModel=[])
    collections.update(extra)
    return Db(**collections)


# ---- the numbers ---------------------------------------------------------------------------------------------------------

def test_pass_percent_grades_and_cgpa_come_from_the_latest_semester():
    out = overview(database([record("S1", 1, ("A", "B"), 8.0), record("S2", 1, ("A", "U"), 6.0),
                             record("S3", 1, ("B", "C"), 7.0)]))
    pass_tile = out["tiles"]["pass_percent"]
    assert out["scope"]["semester"] == 1 and out["scope"]["semester_defaulted"] is True
    assert pass_tile["value"] == 66.7 and pass_tile["of"] == 3 and pass_tile["passed"] == 2
    assert out["tiles"]["average_cgpa"]["value"] == 7.0
    assert {g["grade"]: g["count"] for g in out["grade_distribution"]["items"]} == {"A": 2, "B": 2, "C": 1, "U": 1}
    assert [g["grade"] for g in out["grade_distribution"]["items"]] == ["A", "B", "C", "U"]


def test_the_previous_semester_is_returned_for_comparison():
    out = overview(database([record("S1", 1, ("A",), 8.0), record("S2", 1, ("U",), 4.0),
                             record("S1", 2, ("A",), 8.2), record("S2", 2, ("B",), 5.5)]))
    assert out["tiles"]["pass_percent"]["value"] == 100.0 and out["tiles"]["pass_percent"]["previous"] == 50.0
    assert out["tiles"]["average_cgpa"]["value"] == 6.85 and out["tiles"]["average_cgpa"]["previous"] == 6.0


def test_with_no_results_the_numbers_are_none_with_a_reason_never_zero():
    out = overview(database())
    for name in ("pass_percent", "average_cgpa", "backlogs", "at_risk"):
        assert out["tiles"][name]["value"] is None and out["tiles"][name]["reason"] == "no_results"
    assert out["pass_trend"]["points"] == [] and out["grade_distribution"]["items"] == []
    assert out["scope"]["semester"] is None and out["as_of"] is None
    assert out["tiles"]["students"]["value"] == 0                  # a real zero is still a zero


def test_backlogs_and_at_risk_follow_the_rules():
    out = overview(database([
        record("S1", 1, ("A", "B"), 8.0),                          # fine
        record("S2", 1, ("U", "B"), 6.5),                          # one backlog, not at risk
        record("S3", 1, ("U", "F", "A"), 6.0),                     # two failed papers: at risk
        record("S4", 1, ("A", "B"), 4.5)]))                        # CGPA under the minimum: at risk
    assert out["tiles"]["backlogs"]["value"] == 2 and out["tiles"]["backlogs"]["papers"] == 3
    assert out["tiles"]["backlogs"]["of"] == 4
    assert out["tiles"]["at_risk"]["value"] == 2


def test_failed_papers_count_across_every_semester_and_the_latest_cgpa_is_used():
    out = overview(database([record("S1", 1, ("U",), 4.0), record("S1", 2, ("U",), 8.0)]))
    assert out["tiles"]["at_risk"]["value"] == 1                    # two failed papers over two semesters


def test_a_record_with_no_grades_is_not_counted_as_a_pass_or_a_fail():
    out = overview(database([record("S1", 1, (), 0.0), record("S2", 1, ("A",), 8.0)]))
    assert out["tiles"]["pass_percent"]["of"] == 1 and out["tiles"]["pass_percent"]["value"] == 100.0


def test_the_pass_trend_follows_one_batch_and_names_it():
    out = overview(database([
        record("S1", 1, ("A",), 8.0), record("S1", 2, ("U",), 5.0),
        record("S9", 1, ("U",), 4.0, batch=OLD_BATCH), record("S9", 2, ("U",), 4.0, batch=OLD_BATCH)]))
    assert out["pass_trend"]["batch"] == "CSE 2023"                 # the latest batch with results
    assert [(p["semester"], p["pass_pct"]) for p in out["pass_trend"]["points"]] == [(1, 100.0), (2, 0.0)]
    chosen = overview(database([record("S1", 1, ("A",)), record("S9", 1, ("U",), batch=OLD_BATCH)]), batch=str(OLD_BATCH))
    assert chosen["pass_trend"]["batch"] == "CSE 2022" and chosen["pass_trend"]["points"][0]["pass_pct"] == 0.0


# ---- scope ---------------------------------------------------------------------------------------------------------------

def test_an_explicit_semester_overrides_the_default():
    db = database([record("S1", 1, ("A",)), record("S1", 2, ("U",))])
    out = overview(db, semester=1)
    assert out["scope"]["semester"] == 1 and out["scope"]["semester_defaulted"] is False
    assert out["tiles"]["pass_percent"]["value"] == 100.0


def test_programme_and_batch_filters_narrow_the_numbers():
    db = database([record("S1", 1, ("A",)), record("S2", 1, ("U",), batch=OTHER_PROGRAMME_BATCH)])
    assert overview(db)["tiles"]["pass_percent"]["of"] == 2
    assert overview(db, programme=str(PROGRAMME))["tiles"]["pass_percent"]["value"] == 100.0
    assert overview(db, programme=str(OTHER_PROGRAMME))["tiles"]["pass_percent"]["value"] == 0.0
    assert overview(db, batch=str(BATCH))["tiles"]["pass_percent"]["of"] == 1


def test_an_unknown_batch_and_bad_ids_are_refused():
    with pytest.raises(HTTPException) as error:
        overview(database(), batch=str(ObjectId()))
    assert error.value.status_code == 404
    for scope in ({"programme": "nope"}, {"batch": "nope"}, {"semester": 0}):
        with pytest.raises(HTTPException) as error:
            overview(database(), **scope)
        assert error.value.status_code == 400


def test_the_rules_in_force_are_returned_so_the_screen_never_hard_codes_them():
    rules = overview(database())["rules"]
    assert rules == {"fail_grades": ["F", "U"], "pass_target_percent": 60.0, "at_risk_failed_papers": 2,
                     "at_risk_min_cgpa": 5.0}


def test_the_filters_list_programmes_batches_and_semesters():
    out = overview(database([record("S1", 3, ("A",))]))
    assert {p["name"] for p in out["filters"]["programmes"]} == {"B.Tech CSE", "MBA"}
    assert {b["name"] for b in out["filters"]["batches"]} == {"CSE 2023", "CSE 2022", "MBA 2023"}
    assert out["filters"]["semesters"] == list(range(1, 9))


# ---- only this institute, only colleges, only fresh results ------------------------------------------------------------

def test_another_institutes_records_are_never_counted():
    other = ObjectId()
    db = database([record("S1", 1, ("A",)), record("S2", 1, ("U",), institute=OTHER_INSTITUTE)],
                  studentDetails=[{"_id": ObjectId(), "institute_id": OTHER_INSTITUTE, "is_deleted": False}],
                  subjectDetails=[{"_id": other, "institute_id": OTHER_INSTITUTE}])
    out = overview(db)
    assert out["tiles"]["pass_percent"]["of"] == 1 and out["tiles"]["students"]["value"] == 0
    assert out["tiles"]["data_checks"]["value"] is None             # the other institute's course is not checked


def test_a_school_institute_is_refused():
    with pytest.raises(HTTPException) as error:
        overview(database(institute_type="school"))
    assert error.value.status_code == 403


def test_a_missing_institute_type_reads_as_college():
    db = database()
    db.c["instituteDetails"].docs[0].pop("institute_type")
    assert overview(db)["tiles"]["students"]["value"] == 0


def test_results_that_need_regenerating_are_left_out_and_counted_as_a_warning():
    db = database([record("S1", 1, ("A",)), record("S2", 1, ("U",), stale=True)],
                  subjectDetails=[{"_id": ObjectId(), "institute_id": INSTITUTE, "co_po_matrix": {"CO1": {"PO1": 3}}}])
    out = overview(db)
    assert out["tiles"]["pass_percent"]["of"] == 1
    assert out["tiles"]["data_checks"]["stale"] == 1
    assert any(item["key"] == "stale_results" for item in out["attention"]["items"])


# ---- data checks, evaluation, attention ----------------------------------------------------------------------------------

def _course(co_po=None, institute=INSTITUTE, **extra):
    doc = {"_id": ObjectId(), "institute_id": institute, "programme_id": PROGRAMME, "co_po_matrix": co_po or {}}
    doc.update(extra)
    return doc


def test_data_checks_count_missing_co_mapping_and_untagged_exams():
    course_a, course_b = _course({"CO1": {"PO1": 3}}), _course({})
    exams = [{"_id": ObjectId(), "subject_id": course_a["_id"], "semester": 1, "covered_cos": ["CO1"]},
             {"_id": ObjectId(), "subject_id": course_b["_id"], "semester": 1, "covered_cos": []},
             {"_id": ObjectId(), "subject_id": course_b["_id"], "semester": 1, "covered_cos": ["CO1"],
              "is_archived": True}]
    out = overview(database(subjectDetails=[course_a, course_b], newsavedDocs=exams))
    checks = out["tiles"]["data_checks"]
    assert (checks["courses"], checks["exams"], checks["value"]) == (1, 1, 2)
    assert out["tiles"]["exams"]["value"] == 2                      # the archived exam is not counted
    keys = [item["key"] for item in out["attention"]["items"]]
    assert keys == ["no_co_mapping", "exams_without_cos"] and out["attention"]["all_clear"] is False


def test_evaluation_counts_done_and_pending_answer_sheets():
    course = _course({"CO1": {"PO1": 3}})
    exam = {"_id": ObjectId(), "subject_id": course["_id"], "semester": 1, "covered_cos": ["CO1"]}
    answers = [{"_id": ObjectId(), "exam_id": exam["_id"], "evaluation_status": "evaluated"},
               {"_id": ObjectId(), "exam_id": exam["_id"], "evaluation_status": "faculty_reviewed"},
               {"_id": ObjectId(), "exam_id": exam["_id"], "evaluation_status": "pending_review"}]
    out = overview(database(subjectDetails=[course], newsavedDocs=[exam], answerDetails=answers))
    evaluation = out["tiles"]["evaluation"]
    assert (evaluation["value"], evaluation["of"], evaluation["pending"]) == (2, 3, 1)
    assert [i["key"] for i in out["attention"]["items"]] == ["evaluation_pending"]


def test_with_no_answer_sheets_evaluation_is_none_not_zero():
    out = overview(database())
    assert out["tiles"]["evaluation"]["value"] is None and out["tiles"]["evaluation"]["reason"] == "no_answers"
    assert out["tiles"]["data_checks"]["value"] is None and out["attention"]["all_clear"] is False


def test_all_clear_only_when_the_checks_ran_and_found_nothing():
    course = _course({"CO1": {"PO1": 3}})
    exam = {"_id": ObjectId(), "subject_id": course["_id"], "semester": 1, "covered_cos": ["CO1"]}
    out = overview(database([record("S1", 1, ("A",), 8.0)], subjectDetails=[course], newsavedDocs=[exam]))
    assert out["attention"]["items"] == [] and out["attention"]["all_clear"] is True


def test_programmes_under_the_pass_target_are_listed_by_name():
    db = database([record("S1", 1, ("A",)), record("S2", 1, ("U",), batch=OTHER_PROGRAMME_BATCH),
                   record("S3", 1, ("U",), batch=OTHER_PROGRAMME_BATCH)])
    item = next(i for i in overview(db)["attention"]["items"] if i["key"] == "low_pass_programmes")
    assert item["count"] == 1 and item["names"] == ["MBA"] and item["report"] == "result-analysis"


def test_students_are_counted_by_programme_and_by_batch():
    s1, s2, s3 = (ObjectId() for _ in range(3))
    students = [{"_id": s1, "institute_id": INSTITUTE, "programme_id": PROGRAMME, "is_deleted": False},
                {"_id": s2, "institute_id": INSTITUTE, "programme_id": OTHER_PROGRAMME, "is_deleted": False},
                {"_id": s3, "institute_id": INSTITUTE, "programme_id": PROGRAMME, "is_deleted": True}]
    relations = [{"_id": ObjectId(), "student_id": s1, "batch_id": BATCH},
                 {"_id": ObjectId(), "student_id": s2, "batch_id": OTHER_PROGRAMME_BATCH}]
    db = database(studentDetails=students, StudentSubjectRelationModel=relations)
    assert overview(db)["tiles"]["students"]["value"] == 2          # the deleted student is not counted
    assert overview(db, programme=str(PROGRAMME))["tiles"]["students"]["value"] == 1
    assert overview(db, batch=str(OTHER_PROGRAMME_BATCH))["tiles"]["students"]["value"] == 1


def test_as_of_is_the_newest_result_record_and_nothing_is_written():
    when = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)
    db = database([record("S1", 1, ("A",), updated_at=datetime(2026, 11, 1, tzinfo=timezone.utc)),
                   record("S2", 1, ("A",), updated_at=when)])
    out = overview(db)
    assert out["as_of"] == when.isoformat()
    assert all(col.updates == 0 for col in db.c.values())


# ---- who may ask ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("role", [1, 3, 4, 5, 6, 7, None])
def test_only_an_institute_admin_may_use_the_route(role):
    with pytest.raises(HTTPException) as error:
        run(router.reports_overview(identity={"role": role, "user_id": str(ADMIN_USER)}, db=database()))
    assert error.value.status_code == 403


def test_the_route_returns_the_admins_own_overview():
    out = run(router.reports_overview(programme=None, batch=None, semester=None,
                                      identity={"role": 2, "user_id": str(ADMIN_USER)},
                                      db=database([record("S1", 1, ("A",))])))
    assert out["tiles"]["pass_percent"]["value"] == 100.0


def test_a_user_without_an_institute_profile_is_refused():
    db = database()
    db.c["instituteDetails"].docs.clear()
    with pytest.raises(HTTPException) as error:
        run(router.reports_overview(programme=None, batch=None, semester=None,
                                    identity={"role": 2, "user_id": str(ADMIN_USER)}, db=db))
    assert error.value.status_code == 400
