"""Step A1 stage 3: absent / malpractice / mandatory in results, CO attainment and the not-cleared list."""
import io

from bson import ObjectId
from openpyxl import load_workbook

from app.services.attendance import attendance_status, counts_for_attainment, is_not_cleared
from app.services.co_excel import fetch_exams
from tests.test_security_fixes import _get_institute_id, _register_institute_admin


def _answer(exam_id, sid, marks, status=None, note=None):
    doc = {
        "exam_id": exam_id, "filename": f"MANUAL-{sid}", "student_id": sid, "student_name": sid,
        "total_final_marks": marks, "total_ai_marks": marks, "total_max_marks": 100,
        "questionwise_marking": [{"question_no": 1, "max_marks": 100, "ai_awarded_marks": marks,
            "final_marks": marks, "flags": {},
            "cos": [{"co_code": "CO1", "ai_marks": marks, "max_marks": 100}]}],
    }
    if status:
        doc["attendance_status"] = status
        doc["status_note"] = note
    return doc


async def _setup(superadmin_client, client_factory, test_db, ca_mandatory=False, ca_minimum=None):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Attendance Institute")
    institute_id = ObjectId(await _get_institute_id(institute))
    faculty_id = ObjectId()
    school_id = ObjectId()
    await test_db["schoolDetails"].insert_one({"_id": school_id, "institute_id": institute_id})
    subject = await test_db["subjectDetails"].insert_one({
        "institute_id": institute_id, "school_id": school_id, "programme_id": ObjectId(),
        "faculty_id": faculty_id, "subject_name": "Bio", "subject_code": "B1", "semester": 1,
        "credits": 3, "is_deleted": False,
    })
    base = {"subject_id": subject.inserted_id, "faculty_id": faculty_id, "school_id": school_id,
            "programme_id": ObjectId(), "semester": 1}
    ca = await test_db["newsavedDocs"].insert_one({
        **base, "folder_name": "CA1", "exam_type": "CA", "weightage": 40, "exam_date": None,
        "is_mandatory": ca_mandatory, "min_clear_percent": ca_minimum})
    end = await test_db["newsavedDocs"].insert_one({
        **base, "folder_name": "End", "exam_type": "End", "weightage": 60, "exam_date": None})
    return institute, subject.inserted_id, ca.inserted_id, end.inserted_id


async def _result(institute, subject_id):
    resp = await institute.get(f"/subject/result/{subject_id}")
    assert resp.status_code == 200, resp.text
    return {s["student_id"]: s for s in resp.json()["students"]}, resp.json()


# ---- pure rules -------------------------------------------------------------

def test_rules_default_to_present_and_not_mandatory():
    assert attendance_status({}) == "present" and attendance_status({"attendance_status": "weird"}) == "present"
    assert counts_for_attainment({}) and not counts_for_attainment({"attendance_status": "absent"})
    assert not counts_for_attainment({"attendance_status": "malpractice"})
    assert is_not_cleared({}, "absent", None) is False          # not mandatory: never "not cleared"
    mandatory = {"is_mandatory": True, "min_clear_percent": 40}
    assert is_not_cleared(mandatory, "absent", None) and is_not_cleared(mandatory, "malpractice", 0)
    assert is_not_cleared(mandatory, "present", 39.9) and not is_not_cleared(mandatory, "present", 40)
    assert not is_not_cleared(mandatory, "present", None)       # marks pending: unknown, not a failure
    assert not is_not_cleared({"is_mandatory": True, "min_clear_percent": None}, "present", 0)


# ---- regression: exams with no statuses are unchanged --------------------------

async def test_results_without_statuses_are_unchanged(superadmin_client, client_factory, test_db):
    institute, subject_id, ca, end = await _setup(superadmin_client, client_factory, test_db)
    await test_db["answerDetails"].insert_many([_answer(ca, "S1", 80), _answer(end, "S1", 60)])
    students, body = await _result(institute, subject_id)
    s1 = students["S1"]
    assert s1["results"][str(ca)]["status"] == "evaluated" and s1["results"][str(ca)]["percentage"] == 80.0
    assert s1["results"][str(ca)]["attendance_status"] == "present"
    assert s1["results"][str(ca)]["not_cleared"] is False
    assert s1["composite_percentage"] == 68.0 and s1["is_partial"] is False  # 80*.4 + 60*.6
    assert s1["not_cleared_exam_ids"] == []
    assert s1["co_summary"] == [{"co_code": "CO1", "obtained_marks": 140.0, "max_marks": 200.0, "percentage": 70.0}]
    assert all(e["is_mandatory"] is False and e["min_clear_percent"] is None for e in body["exams"])


# ---- absent -------------------------------------------------------------------

async def test_absent_on_a_normal_exam_is_left_out_not_pending(superadmin_client, client_factory, test_db):
    institute, subject_id, ca, end = await _setup(superadmin_client, client_factory, test_db)
    await test_db["answerDetails"].insert_many(
        [_answer(ca, "S1", 0, "absent"), _answer(end, "S1", 50)])
    s1 = (await _result(institute, subject_id))[0]["S1"]
    cell = s1["results"][str(ca)]
    assert cell["status"] == "absent" and cell["final_marks"] is None and cell["percentage"] is None
    assert s1["composite_percentage"] == 50.0 and s1["is_partial"] is False
    assert s1["co_summary"][0]["max_marks"] == 100.0          # only the End exam feeds CO totals
    assert s1["not_cleared_exam_ids"] == []                   # CA is not mandatory


async def test_absent_on_a_mandatory_exam_keeps_the_result_incomplete(superadmin_client, client_factory, test_db):
    institute, subject_id, ca, end = await _setup(
        superadmin_client, client_factory, test_db, ca_mandatory=True, ca_minimum=40)
    await test_db["answerDetails"].insert_many(
        [_answer(ca, "S1", 0, "absent"), _answer(end, "S1", 90)])
    s1 = (await _result(institute, subject_id))[0]["S1"]
    assert s1["is_partial"] is True                            # no course grade until cleared
    assert s1["not_cleared_exam_ids"] == [str(ca)]
    assert s1["results"][str(ca)]["not_cleared"] is True


# ---- malpractice ----------------------------------------------------------------

async def test_malpractice_is_a_real_zero_but_not_in_attainment(superadmin_client, client_factory, test_db):
    institute, subject_id, ca, end = await _setup(superadmin_client, client_factory, test_db)
    await test_db["answerDetails"].insert_many(
        [_answer(ca, "S1", 80), _answer(end, "S1", 0, "malpractice", "phone")])
    s1 = (await _result(institute, subject_id))[0]["S1"]
    cell = s1["results"][str(end)]
    assert cell["status"] == "malpractice" and cell["percentage"] == 0.0 and cell["status_note"] == "phone"
    assert s1["composite_percentage"] == 32.0                  # 80*.4 + 0*.6
    assert s1["co_summary"][0]["max_marks"] == 100.0           # CA only


# ---- mandatory minimum ------------------------------------------------------------

async def test_below_the_minimum_is_flagged_and_above_is_not(superadmin_client, client_factory, test_db):
    institute, subject_id, ca, end = await _setup(
        superadmin_client, client_factory, test_db, ca_mandatory=True, ca_minimum=40)
    await test_db["answerDetails"].insert_many(
        [_answer(ca, "LOW", 30), _answer(ca, "OK", 40), _answer(ca, "HIGH", 75)])
    students, body = await _result(institute, subject_id)
    assert students["LOW"]["not_cleared_exam_ids"] == [str(ca)]
    assert students["OK"]["not_cleared_exam_ids"] == [] and students["HIGH"]["not_cleared_exam_ids"] == []
    ca_meta = next(e for e in body["exams"] if e["exam_id"] == str(ca))
    assert ca_meta["is_mandatory"] is True and ca_meta["min_clear_percent"] == 40.0


# ---- CO attainment report + not-cleared endpoint ----------------------------------

async def test_co_report_ignores_absent_and_malpractice_students(superadmin_client, client_factory, test_db):
    institute, subject_id, ca, end = await _setup(superadmin_client, client_factory, test_db)
    await test_db["answerDetails"].insert_many([
        _answer(ca, "S1", 80), _answer(ca, "S2", 0, "absent"), _answer(ca, "S3", 0, "malpractice")])
    exams = await fetch_exams(test_db, str(subject_id))
    ca_exam = next(e for e in exams if e["exam_id"] == str(ca))
    assert [s["student_name"] for s in ca_exam["students"]] == ["S1"]


async def test_not_cleared_endpoint_lists_the_right_students(superadmin_client, client_factory, test_db):
    institute, subject_id, ca, end = await _setup(
        superadmin_client, client_factory, test_db, ca_mandatory=True, ca_minimum=40)
    await test_db["answerDetails"].insert_many([
        _answer(ca, "PASS", 70), _answer(ca, "LOW", 20), _answer(ca, "AB", 0, "absent"),
        _answer(ca, "MP", 0, "malpractice", "copied")])
    resp = await institute.get(f"/not-cleared/{ca}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_mandatory"] is True and body["min_clear_percent"] == 40.0 and body["count"] == 3
    assert {(s["student_id"], s["reason"]) for s in body["students"]} == {
        ("LOW", "below_minimum"), ("AB", "absent"), ("MP", "malpractice")}

    other = await institute.get(f"/not-cleared/{end}")           # not a mandatory exam
    assert other.status_code == 200 and other.json()["students"] == [] and other.json()["is_mandatory"] is False


def test_detailed_excel_shows_ab_and_mp():
    from app.api.routers.subject_results import _build_detailed_excel_workbook
    exam = ObjectId()
    answers = [_answer(exam, "S1", 80), _answer(exam, "S2", 0, "absent"), _answer(exam, "S3", 0, "malpractice")]
    workbook = _build_detailed_excel_workbook({"question_paper": {"no_of_questions": 1}}, [], answers)
    sheet = load_workbook(io.BytesIO(workbook)).active
    rows = {row[2].value: [c.value for c in row] for row in sheet.iter_rows(min_row=4) if row[2].value}
    assert "AB" in rows["S2"] and "MP" in rows["S3"] and "AB" not in rows["S1"] and "MP" not in rows["S1"]
    assert 80 in rows["S1"] or 80.0 in rows["S1"]
