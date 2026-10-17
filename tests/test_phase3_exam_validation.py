"""Exam/rubric input regressions: isolated app, no external DB or providers."""

from copy import deepcopy

import pytest
from bson import ObjectId

from tests.test_answer_access import isolated


def form():
    return {"folderName": "Data Science - ESA", "school": str(ObjectId()),
        "programme": str(ObjectId()), "subject_id": str(ObjectId()),
        "semester": "5", "examtype": "End semester", "examdetails": "ESA",
        "examdate": "2026-10-17T10:30", "weightage": "60", "covered_cos": '["CO1"]'}


BAD_FIELDS = [
    ("semester", "abc"), ("semester", "0"), ("semester", "-1"), ("semester", "1.5"),
    ("semester", "NaN"), ("semester", "Infinity"),
    ("weightage", "abc"), ("weightage", "-1"), ("weightage", "101"),
    ("weightage", "NaN"), ("weightage", "Infinity"),
    ("examdate", "invalid"), ("examdate", "2026-02-30T10:30"), ("examdate", ""),
    ("examdate", "2026-10-17"), ("covered_cos", '["CO1"'),
    ("covered_cos", '[1]'), ("covered_cos", '["CO1", "CO1"]'),
    ("covered_cos", '[""]'), ("school", "invalid"), ("department", "invalid"),
    ("covered_cos", '{}'), ("covered_cos", 'null'), ("covered_cos", '"CO1"'),
    ("folderName", "  "), ("examtype", "  "), ("examdetails", "  "),
    ("is_course_exit_summary", "maybe"),
]


@pytest.mark.parametrize("operation", ["create", "edit"])
@pytest.mark.parametrize("field,value", BAD_FIELDS)
async def test_invalid_form_rejected_before_writes(isolated, operation, field, value):
    before = {name: deepcopy(col.documents) for name, col in isolated.db.items()}
    data = {**form(), field: value} if operation == "create" else {field: value}
    if operation == "create":
        response = await isolated.client.post("/createSaveFolder", data=data, headers={"x-test-user": "owner"})
    else:
        response = await isolated.client.put(f"/update-exam/{isolated.exam}", data=data, headers={"x-test-user": "owner"})
    assert response.status_code == 400, response.text
    assert isinstance(response.json()["detail"], str)
    assert before == {name: col.documents for name, col in isolated.db.items()}
    assert not any(col.writes for col in isolated.db.values())


@pytest.mark.parametrize("operation", ["create", "edit"])
@pytest.mark.parametrize("weightage,expected", [("", 0), ("0", 0), ("12.5", 12.5), ("100", 100)])
async def test_valid_weightage_normalized_consistently(isolated, operation, weightage, expected):
    data = {**form(), "weightage": weightage}
    if operation == "create":
        response = await isolated.client.post("/createSaveFolder", data=data, headers={"x-test-user": "owner"})
        doc = isolated.db["newsavedDocs"].documents[-1]
    else:
        response = await isolated.client.put(f"/update-exam/{isolated.exam}", data=data, headers={"x-test-user": "owner"})
        doc = isolated.db["newsavedDocs"].documents[0]
    assert response.status_code == 200, response.text
    assert doc["weightage"] == expected
    assert isinstance(doc["weightage"], (float, int))
    assert doc["semester"] == 5
    assert doc["covered_cos"] == ["CO1"]
    assert doc["exam_date"].isoformat() == "2026-10-17T10:30:00"


@pytest.mark.parametrize("co_input", ['[" CO1 ", "CO2"]', "CO1, CO2"])
async def test_edit_co_list_can_be_used_in_rubric(isolated, co_input):
    response = await isolated.client.put(f"/update-exam/{isolated.exam}",
        data={"covered_cos": co_input}, headers={"x-test-user": "owner"})
    assert response.status_code == 200
    assert isolated.db["newsavedDocs"].documents[0]["covered_cos"] == ["CO1", "CO2"]
    response = await isolated.client.post(f"/evaluation-details/{isolated.exam}",
        json={"totalMarks": 10, "questionEvaluationDetails": [rubric_question()]},
        headers={"x-test-user": "owner"})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("legacy", ["[", "{}", '[{"maxMarks":"bad"}]'])
async def test_bad_embedded_create_rubric_does_not_leave_exam(isolated, legacy):
    response = await isolated.client.post("/createSaveFolder",
        data={**form(), "questionEvaluationDetails": legacy}, headers={"x-test-user": "owner"})
    assert response.status_code == 400, response.text
    assert not any(col.writes for col in isolated.db.values())


async def test_date_read_edit_roundtrip_keeps_wall_time(isolated):
    for _ in range(2):
        response = await isolated.client.put(f"/update-exam/{isolated.exam}",
            data={"examdate": "2026-10-17T10:30"}, headers={"x-test-user": "owner"})
        assert response.status_code == 200
        response = await isolated.client.get(f"/newsaved-documents/{isolated.exam}", headers={"x-test-user": "owner"})
        assert response.json()["exam_date"] == "2026-10-17T10:30:00"


def rubric_question(**changes):
    return {"maxMarks": 10, "minMarks": 0, "guidelines": "Check the working",
        "parameters": [{"name": "Accuracy", "percentage": 100}],
        "cos": [{"co_code": "CO1", "marks": 10}], **changes}


BAD_QUESTIONS = [
    rubric_question(maxMarks="five"), rubric_question(maxMarks="NaN"),
    rubric_question(maxMarks="Infinity"), rubric_question(maxMarks=True),
    rubric_question(minMarks=-1), rubric_question(minMarks=11), rubric_question(minMarks="NaN"),
    rubric_question(parameters=None), rubric_question(parameters=[None]),
    rubric_question(parameters=[{"name": "   ", "percentage": 100}]),
    rubric_question(parameters=[{"name": "Accuracy", "percentage": 120}]),
    rubric_question(parameters=[{"name": "Accuracy", "percentage": 50}]),
    rubric_question(parameters=[{"name": "Accuracy", "percentage": "NaN"}]),
    rubric_question(parameters=[{"name": "Accuracy", "percentage": True}]),
    rubric_question(parameters=[{"name": "Accuracy", "percentage": 50}] * 2),
    rubric_question(cos=None), rubric_question(cos=[None]),
    rubric_question(cos=[{"co_code": "CO1", "marks": 12}]),
    rubric_question(cos=[{"co_code": "CO1", "marks": 5}]),
    rubric_question(cos=[{"co_code": "CO1", "marks": "NaN"}]),
    rubric_question(cos=[{"co_code": "CO1", "marks": -1}]),
    rubric_question(cos=[{"co_code": "CO1", "marks": 5}] * 2),
    rubric_question(cos=[{"co_code": "", "marks": 10}]),
    rubric_question(cos=[{"co_code": "CO1", "marks": 10, "description": None}]),
    rubric_question(guidelines=None),
]


@pytest.mark.parametrize("question", BAD_QUESTIONS)
@pytest.mark.parametrize("has_saved", [False, True])
async def test_bad_rubric_does_not_insert_or_replace(isolated, question, has_saved):
    isolated.db["newsavedDocs"].documents[0]["covered_cos"] = ["CO1"]
    if has_saved:
        isolated.db["evaluationDetails"].documents.append({
            "_id": ObjectId(), "exam_id": ObjectId(isolated.exam), "faculty_id": isolated.owner_faculty,
            "totalMarks": 10, "questionEvaluationDetails": [rubric_question()]})
    before = deepcopy(isolated.db["evaluationDetails"].documents)
    response = await isolated.client.post(f"/evaluation-details/{isolated.exam}",
        json={"totalMarks": 10, "questionEvaluationDetails": [question]}, headers={"x-test-user": "owner"})
    assert response.status_code == 400, response.text
    assert isolated.db["evaluationDetails"].documents == before
    assert not isolated.db["evaluationDetails"].writes


@pytest.mark.parametrize("total,status", [(5, 400), (20, 400), (0, 422), (True, 422), ("NaN", 422), ("Infinity", 422)])
async def test_rubric_total_checked_against_questions(isolated, total, status):
    isolated.db["newsavedDocs"].documents[0]["covered_cos"] = ["CO1"]
    response = await isolated.client.post(f"/evaluation-details/{isolated.exam}",
        json={"totalMarks": total, "questionEvaluationDetails": [rubric_question()]}, headers={"x-test-user": "owner"})
    assert response.status_code == status, response.text
    assert not isolated.db["evaluationDetails"].writes


@pytest.mark.parametrize("legacy_cos", [["CO1", "CO2"], '["CO1", "CO2"]'])
async def test_decimal_rubric_roundtrip_and_legacy_exam_cos(isolated, legacy_cos):
    isolated.db["newsavedDocs"].documents[0]["covered_cos"] = legacy_cos
    questions = [rubric_question(maxMarks=2.5, minMarks=0.5,
        parameters=[{"name": "Accuracy", "percentage": 33.3}, {"name": "Reasoning", "percentage": 66.7}],
        cos=[{"co_code": "CO1", "marks": 1}, {"co_code": "CO2", "marks": 1.5}])]*2
    for _ in range(2):
        response = await isolated.client.post(f"/evaluation-details/{isolated.exam}",
            json={"totalMarks": 5, "questionEvaluationDetails": questions}, headers={"x-test-user": "owner"})
        assert response.status_code == 200, response.text
    response = await isolated.client.get(f"/evaluation-details/{isolated.exam}", headers={"x-test-user": "owner"})
    assert response.status_code == 200
    assert response.json()["evaluation"]["totalMarks"] == 5
    assert len(isolated.db["evaluationDetails"].documents) == 1


async def test_rubric_without_optional_cos_or_parameters_is_supported(isolated):
    response = await isolated.client.post(f"/evaluation-details/{isolated.exam}",
        json={"totalMarks": 10, "questionEvaluationDetails": [rubric_question(cos=[], parameters=[])]},
        headers={"x-test-user": "owner"})
    assert response.status_code == 200, response.text
