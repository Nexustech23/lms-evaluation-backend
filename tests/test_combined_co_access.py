"""Subject report authorization; in-memory records only."""
from copy import deepcopy
from unittest.mock import Mock

import pytest
from bson import ObjectId

from tests.test_answer_access import Collection, isolated


def seed_subject(s):
    faculty = s.db["facultyDetails"].documents[0]
    subject = {"_id": ObjectId(), "faculty_id": faculty["_id"], "institute_id": faculty["institute_id"]}
    s.db["subjectDetails"] = Collection([subject])
    s.db["newsavedDocs"].documents[0].update(subject_id=subject["_id"], weightage=50)
    s.db["answerDetails"].documents[0].update(student_name="Demo student", questionwise_marking=[{
        "question_no": 1, "cos": [{"co_code": "CO1", "final_co_marks": 8, "max_marks": 10}],
    }])
    return s.db["subjectDetails"].documents[0]


@pytest.mark.parametrize("user,status", [
    (None, 401), ("student", 403), ("learner", 403), ("superadmin", 403),
    ("other_faculty", 404), ("foreign_faculty", 404), ("foreign_admin", 404),
])
async def test_unrelated_accounts_cannot_read_combined_co(isolated, monkeypatch, user, status):
    subject = seed_subject(isolated)
    before = {k: deepcopy(v.documents) for k, v in isolated.db.items()}
    scan = Mock(wraps=isolated.db["newsavedDocs"].find)
    monkeypatch.setattr(isolated.db["newsavedDocs"], "find", scan)
    response = await isolated.client.get(f'/combinedCO/{subject["_id"]}',
                                         headers={"x-test-user": user} if user else {})
    assert response.status_code == status
    assert "Demo student" not in response.text and "obtained_marks" not in response.text
    scan.assert_not_called()
    assert before == {k: v.documents for k, v in isolated.db.items()}


@pytest.mark.parametrize("user", ["owner", "admin"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_assigned_faculty_and_own_institute_can_read(isolated, user, legacy):
    subject = seed_subject(isolated)
    if legacy:
        subject["faculty_id"] = str(subject["faculty_id"])
        subject["institute_id"] = str(subject["institute_id"])
    response = await isolated.client.get(f'/combinedCO/{subject["_id"]}', headers={"x-test-user": user})
    assert response.status_code == 200, response.text
    student = response.json()["exams"][0]["students"][0]
    assert student["student_name"] == "Demo student"
    assert student["questions"][0]["cos"][0]["obtained_marks"] == 8
    assert not any(c.writes for c in isolated.db.values())


@pytest.mark.parametrize("subject_id,status", [("bad-id", 400), (str(ObjectId()), 404)])
async def test_bad_or_missing_subject(isolated, subject_id, status):
    seed_subject(isolated)
    response = await isolated.client.get(f'/combinedCO/{subject_id}', headers={"x-test-user": "owner"})
    assert response.status_code == status


@pytest.mark.parametrize("user", ["owner", "admin"])
@pytest.mark.parametrize("broken", ["deleted", "missing_tenant", "invalid_tenant"])
async def test_deleted_or_unscoped_subject_is_denied(isolated, user, broken):
    subject = seed_subject(isolated)
    if broken == "deleted":
        subject["is_deleted"] = True
    else:
        subject["institute_id"] = None if broken == "missing_tenant" else "bad-id"
    response = await isolated.client.get(f'/combinedCO/{subject["_id"]}', headers={"x-test-user": user})
    assert response.status_code == 404


async def test_reassigned_subject_denies_former_faculty(isolated):
    subject = seed_subject(isolated)
    subject["faculty_id"] = isolated.db["facultyDetails"].documents[1]["_id"]
    response = await isolated.client.get(f'/combinedCO/{subject["_id"]}', headers={"x-test-user": "owner"})
    assert response.status_code == 404
    response = await isolated.client.get(f'/combinedCO/{subject["_id"]}', headers={"x-test-user": "other_faculty"})
    assert response.status_code == 200


async def test_matching_faculty_with_foreign_tenant_link_is_denied(isolated):
    subject = seed_subject(isolated)
    subject["institute_id"] = isolated.db["facultyDetails"].documents[2]["institute_id"]
    response = await isolated.client.get(f'/combinedCO/{subject["_id"]}', headers={"x-test-user": "owner"})
    assert response.status_code == 404


async def test_admin_without_profile_is_denied(isolated):
    subject = seed_subject(isolated)
    isolated.db["instituteDetails"].documents.clear()
    response = await isolated.client.get(f'/combinedCO/{subject["_id"]}', headers={"x-test-user": "admin"})
    assert response.status_code == 404
