"""CO / PO Attainment summary. A fake database answers; no real database, no network, no AI. The CO numbers must come from
the same building blocks as the per-course Excel report, so a hand-worked example is checked here: each CO's level from
the share of students at or above the threshold, weighted by the exam weight, and each PO from the CO-PO weights of the
attained COs. Courses that cannot be worked out are listed with a reason and never counted as zero."""
import asyncio
import io

import pytest
from bson import ObjectId
from fastapi import HTTPException
from openpyxl import load_workbook

from app.api.routers import reports_overview as router
from app.services import reports_attainment as ra
from tests.test_reports_overview import ADMIN_USER, BATCH, INSTITUTE, OTHER_INSTITUTE, PROGRAMME, database

TARGETS = [
    {"level": 3, "min_percentage": 70, "max_percentage": 100, "comparision_percentage": 60},
    {"level": 2, "min_percentage": 60, "max_percentage": 69.99, "comparision_percentage": 60},
    {"level": 1, "min_percentage": 40, "max_percentage": 59.99, "comparision_percentage": 60},
]
MATRIX = {"CO1": {"PO1": 3, "PO2": 1}, "CO2": {"PO1": 2}}


def run(coro):
    return asyncio.run(coro)


def attainment(db, **scope):
    return run(ra.build_attainment(db, INSTITUTE, **scope))


def answer(exam, sid, marks_by_co, **extra):
    """An evaluated answer sheet: one question per CO, each out of 100."""
    doc = {"_id": ObjectId(), "exam_id": exam["_id"], "student_id": sid, "student_name": sid, "evaluation_status": "evaluated",
           "questionwise_marking": [
               {"question_no": i + 1, "max_marks": 100, "final_marks": m, "flags": {},
                "cos": [{"co_code": co, "ai_marks": m, "max_marks": 100}]}
               for i, (co, m) in enumerate(marks_by_co.items())]}
    doc.update(extra)
    return doc


def course(name="Maths", matrix=MATRIX, **extra):
    base = {"_id": ObjectId(), "institute_id": INSTITUTE, "programme_id": PROGRAMME, "batch_id": BATCH, "semester": 1,
            "subject_name": name, "subject_code": name[:2].upper(), "co_po_matrix": matrix}
    base.update(extra)
    return base


def exam(subject, weightage=100, **extra):
    base = {"_id": ObjectId(), "subject_id": subject["_id"], "folder_name": "End Sem", "exam_type": "End Semester Examination",
            "weightage": weightage, "semester": 1, "covered_cos": ["CO1", "CO2"]}
    base.update(extra)
    return base


def world(subject, exams, answers, targets=TARGETS, co_target=1.8):
    db = database(subjectDetails=[subject], newsavedDocs=exams, answerDetails=answers)
    db.c["programmeDetails"].docs[0].update({"targets": targets, "coAttainmentTarget": co_target})
    return db


def standard():
    subject = course()
    e = exam(subject)
    marks = [(80, 90), (65, 85), (50, 70), (30, 65)]                 # (CO1, CO2) per student
    return world(subject, [e], [answer(e, f"S{i}", {"CO1": a, "CO2": b}) for i, (a, b) in enumerate(marks)]), subject


# ---- the maths, worked by hand ---------------------------------------------------------------------------------------------

def test_co_scores_follow_the_levels_the_weights_and_the_target():
    db, _ = standard()
    row = attainment(db)["subjects"][0]
    scores = {c["code"]: c for c in row["co"]}
    # CO1: 2 of 4 students are at or above 60 % -> 50 % of students -> level 1 -> 1 x 100/100 = 1.0 (target 1.8: not attained)
    assert (scores["CO1"]["score"], scores["CO1"]["attained"]) == (1.0, False)
    # CO2: all 4 are at or above 60 % -> 100 % of students -> level 3 -> 3.0 (attained)
    assert (scores["CO2"]["score"], scores["CO2"]["attained"], scores["CO2"]["target"]) == (3.0, True, 1.8)
    assert row["status"] == "ok"


def test_po_percent_and_level_come_from_the_weights_of_the_attained_cos():
    db, _ = standard()
    po = attainment(db)["subjects"][0]["po"]
    assert po["PO1"] == {"percent": 40.0, "level": 1.2}               # CO2 (weight 2) attained of 3 + 2
    assert po["PO2"] == {"percent": 0.0, "level": 0.0}                # only CO1 (weight 1), not attained


def test_the_exam_weight_scales_each_exams_share_of_the_final_score():
    subject = course()
    big, small = exam(subject, 75, folder_name="End Sem"), exam(subject, 25, folder_name="Quiz 1", exam_type="Quiz")
    rows = [(80, 90), (65, 85), (50, 70), (30, 65)]
    answers = ([answer(big, f"S{i}", {"CO1": a, "CO2": b}) for i, (a, b) in enumerate(rows)]
               + [answer(small, f"S{i}", {"CO1": 90, "CO2": 90}) for i in range(4)])
    score = {c["code"]: c["score"] for c in attainment(world(subject, [big, small], answers))["subjects"][0]["co"]}
    assert score["CO1"] == 1.5                                        # 1 x 0.75 + 3 x 0.25
    assert score["CO2"] == 3.0


def test_absent_and_malpractice_students_do_not_count_towards_attainment():
    subject = course()
    e = exam(subject)
    answers = [answer(e, f"S{i}", {"CO1": 80, "CO2": 80}) for i in range(2)]
    answers += [answer(e, "A", {"CO1": 0, "CO2": 0}, attendance_status="absent"),
                answer(e, "M", {"CO1": 0, "CO2": 0}, attendance_status="malpractice")]
    score = {c["code"]: c["score"] for c in attainment(world(subject, [e], answers))["subjects"][0]["co"]}
    assert score == {"CO1": 3.0, "CO2": 3.0}                          # the zeros of the two would have dragged this down


# ---- courses that cannot be worked out ------------------------------------------------------------------------------------

def test_a_course_without_answer_sheets_is_listed_with_a_reason_and_not_counted_as_zero():
    subject = course()
    out = attainment(world(subject, [exam(subject)], []))
    assert out["subjects"][0]["status"] == "no_data" and out["counts"]["no_data"] == 1
    assert out["counts"]["computed"] == 0 and out["programmes"] == [] and out["overall"]["co_total"] == 0


def test_a_course_with_a_pending_review_is_skipped_with_its_reason():
    subject = course()
    e = exam(subject)
    out = attainment(world(subject, [e], [answer(e, "S1", {"CO1": 80}, evaluation_status="pending_review")]))
    assert out["subjects"][0]["status"] == "pending_review" and out["counts"]["pending_review"] == 1


def test_a_course_with_no_programme_level_bands_cannot_be_worked_out():
    subject = course()
    e = exam(subject)
    out = attainment(world(subject, [e], [answer(e, "S1", {"CO1": 80})], targets=[]))
    assert out["subjects"][0]["status"] == "no_targets"


def test_a_course_with_no_co_po_matrix_still_shows_its_cos_but_has_no_po_and_is_not_in_the_po_roll_up():
    subject = course(matrix={})
    e = exam(subject)
    out = attainment(world(subject, [e], [answer(e, f"S{i}", {"CO1": 80}) for i in range(3)]))
    row = out["subjects"][0]
    assert row["status"] == "no_matrix" and row["po"] == {} and row["co"][0]["score"] == 3.0
    assert out["counts"]["with_po"] == 0 and out["po_codes"] == [] and out["overall"]["po"] == {}
    assert out["overall"]["co_attained"] == 1                         # its COs still count towards the CO totals


# ---- the roll-ups ------------------------------------------------------------------------------------------------------------

def test_programmes_and_batches_average_the_po_percent_of_the_courses_that_address_it():
    a, b = course("Maths"), course("Physics", matrix={"CO1": {"PO1": 4}, "CO2": {"PO1": 1}})
    ea, eb = exam(a), exam(b)
    good = [(90, 90)] * 4
    answers = ([answer(ea, f"S{i}", {"CO1": x, "CO2": y}) for i, (x, y) in enumerate([(80, 90), (65, 85), (50, 70), (30, 65)])]
               + [answer(eb, f"S{i}", {"CO1": x, "CO2": y}) for i, (x, y) in enumerate(good)])
    db = database(subjectDetails=[a, b], newsavedDocs=[ea, eb], answerDetails=answers)
    db.c["programmeDetails"].docs[0].update({"targets": TARGETS, "coAttainmentTarget": 1.8})
    out = attainment(db)
    assert out["po_codes"] == ["PO1", "PO2"]
    programme = out["programmes"][0]
    assert programme["name"] == "B.Tech CSE" and programme["subjects"] == 2
    # Maths PO1 = 40 %, Physics PO1 = 100 % (both COs attained) -> mean 70 %; PO2 only Maths -> 0 %
    assert programme["po"]["PO1"] == {"percent": 70.0, "level": 2.1, "subjects": 2}
    assert programme["po"]["PO2"]["percent"] == 0.0 and programme["po"]["PO2"]["subjects"] == 1
    assert (programme["co_attained"], programme["co_total"]) == (3, 4)
    assert out["batches"][0]["name"] == "CSE 2023" and out["batches"][0]["po"]["PO1"]["percent"] == 70.0


def test_the_scope_narrows_the_courses_and_an_explicit_semester_is_followed():
    subject_a, subject_b = course("Maths"), course("Physics", semester=2, programme_id=ObjectId())
    ea, eb = exam(subject_a), exam(subject_b, semester=2)
    answers = [answer(ea, "S1", {"CO1": 80, "CO2": 80}), answer(eb, "S1", {"CO1": 80, "CO2": 80})]
    db = database(subjectDetails=[subject_a, subject_b], newsavedDocs=[ea, eb], answerDetails=answers)
    db.c["programmeDetails"].docs[0].update({"targets": TARGETS, "coAttainmentTarget": 1.8})
    assert attainment(db)["subjects_in_scope"] == 2
    assert [s["name"] for s in attainment(db, programme=str(PROGRAMME))["subjects"]] == ["Maths"]
    assert [s["name"] for s in attainment(db, semester=2)["subjects"]] == ["Physics"]


def test_too_many_courses_asks_for_a_narrower_scope_and_works_nothing_out(monkeypatch):
    monkeypatch.setattr(ra, "MAX_SUBJECTS", 2)
    db = database(subjectDetails=[course(f"C{i}") for i in range(3)])
    out = attainment(db)
    assert out["too_many"] is True and out["subjects_in_scope"] == 3 and out["subjects"] == []
    assert run(ra.build_attainment(db, INSTITUTE, programme=str(PROGRAMME), semester=9))["too_many"] is False


def test_another_institutes_courses_are_never_counted_and_no_student_is_named():
    mine, theirs = course("Mine"), course("Theirs", institute_id=OTHER_INSTITUTE)
    e = exam(mine)
    db = world(mine, [e], [answer(e, "S1", {"CO1": 80, "CO2": 80})])
    db.c["subjectDetails"].docs.append(theirs)
    out = attainment(db)
    assert [s["name"] for s in out["subjects"]] == ["Mine"] and "S1" not in repr(out)
    assert all(col.updates == 0 for col in db.c.values())


def test_a_school_institute_is_refused():
    with pytest.raises(HTTPException) as error:
        attainment(database(institute_type="school"))
    assert error.value.status_code == 403


# ---- the Excel export and the routes ------------------------------------------------------------------------------------------

def call(name, db, role=2, **extra):
    args = dict(programme=None, batch=None, semester=None, identity={"role": role, "user_id": str(ADMIN_USER)}, db=db)
    args.update(extra)
    return run(getattr(router, name)(**args))


def test_the_export_carries_the_roll_ups_the_courses_and_a_report_info_sheet():
    db, _ = standard()
    db.c["users"].docs[0]["fullName"] = "Demo University"
    response = call("reports_attainment_export", db)
    assert response.media_type == router.XLSX and "co-po-attainment-" in response.headers["content-disposition"]
    book = load_workbook(io.BytesIO(response.body))
    assert book.sheetnames == ["Programmes", "Batches", "Courses", "Report info"]
    programmes = list(book["Programmes"].iter_rows(min_row=2, values_only=True))[0]
    assert programmes[:4] == ("B.Tech CSE", 1, 1, 2) and programmes[4:] == (40.0, 0.0)
    course_row = list(book["Courses"].iter_rows(min_row=2, values_only=True))[0]
    assert course_row[5] == "Worked out" and "CO2 3.0/1.8 attained" in course_row[6]
    info = {row[0]: row[1] for row in book["Report info"].iter_rows(values_only=True)}
    assert info["Institute"] == "Demo University" and "No PO target" in info["Limits"]


def test_the_export_refuses_a_scope_with_too_many_courses(monkeypatch):
    monkeypatch.setattr(ra, "MAX_SUBJECTS", 1)
    with pytest.raises(HTTPException) as error:
        call("reports_attainment_export", database(subjectDetails=[course("A"), course("B")]))
    assert error.value.status_code == 400


@pytest.mark.parametrize("name", ["reports_attainment_summary", "reports_attainment_export"])
@pytest.mark.parametrize("role", [1, 3, 4, 5, 6, 7, None])
def test_only_an_institute_admin_may_use_the_routes(name, role):
    with pytest.raises(HTTPException) as error:
        call(name, database(), role=role)
    assert error.value.status_code == 403


def test_the_route_returns_the_admins_own_summary():
    db, _ = standard()
    assert call("reports_attainment_summary", db)["counts"]["computed"] == 1
