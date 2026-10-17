"""Read authorization regressions; no production database or storage calls."""

import io
import zipfile
from copy import deepcopy
from unittest.mock import Mock

import pytest
from bson import ObjectId

from app.api.routers import exams
from tests.test_answer_access import isolated


ROUTES = ["newsaved-documents", "download-folder"]


@pytest.fixture
def storage(monkeypatch):
    fetch = Mock(return_value=b"%PDF-1.4\nfake test document")
    monkeypatch.setattr(exams, "safe_get", fetch)
    return fetch


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("user,status", [
    (None, 401), ("other_faculty", 404), ("foreign_faculty", 404),
    ("foreign_admin", 404), ("student", 403), ("learner", 403), ("superadmin", 403),
])
async def test_denied_reads_do_not_scan_answers_or_fetch_files(isolated, storage, monkeypatch, route, user, status):
    scan = Mock(wraps=isolated.db["answerDetails"].find)
    monkeypatch.setattr(isolated.db["answerDetails"], "find", scan)
    before = {name: deepcopy(col.documents) for name, col in isolated.db.items()}
    response = await isolated.client.get(f"/{route}/{isolated.exam}",
        headers={"x-test-user": user} if user else {})
    assert response.status_code == status, response.text
    scan.assert_not_called()
    storage.assert_not_called()
    assert before == {name: col.documents for name, col in isolated.db.items()}
    assert not any(col.writes for col in isolated.db.values())


@pytest.mark.parametrize("user", ["owner", "admin"])
async def test_authorized_details_preserve_response(isolated, storage, user):
    exam = isolated.db["newsavedDocs"].documents[0]
    exam.update(folder_name="Demo exam", question_paper={"url": "https://storage.test/paper.pdf"},
                semester=5, covered_cos=["CO1"], weightage=60)
    response = await isolated.client.get(f"/newsaved-documents/{isolated.exam}", headers={"x-test-user": user})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["id"] == isolated.exam
    assert data["folder_name"] == "Demo exam"
    assert data["question_paper"] == exam["question_paper"]
    assert data["covered_cos"] == ["CO1"]
    assert data["weightage"] == 60
    assert data["created_at"] is None
    storage.assert_not_called()


@pytest.mark.parametrize("user", ["owner", "admin"])
async def test_authorized_zip_contains_only_selected_exam(isolated, storage, user):
    isolated.db["newsavedDocs"].documents[0]["folder_name"] = "Demo"
    isolated.db["answerDetails"].documents[0].update(
        answer_script_url="https://storage.test/answer.pdf",
        evaluated_report_url="https://storage.test/report.pdf")
    isolated.db["answerDetails"].documents.append({
        "_id": ObjectId(), "exam_id": ObjectId(), "filename": "foreign.pdf",
        "answer_script_url": "https://storage.test/foreign.pdf"})
    response = await isolated.client.get(f"/download-folder/{isolated.exam}", headers={"x-test-user": user})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == "attachment; filename=Demo.zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ["student/student.pdf", "student/student_result.pdf"]
        assert all(archive.read(name).startswith(b"%PDF") for name in archive.namelist())
    assert {call.args[0] for call in storage.call_args_list} == {
        "https://storage.test/answer.pdf", "https://storage.test/report.pdf"}


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("exam_id,status", [("invalid", 400), (str(ObjectId()), 404)])
async def test_invalid_or_missing_exam(isolated, storage, route, exam_id, status):
    response = await isolated.client.get(f"/{route}/{exam_id}", headers={"x-test-user": "owner"})
    assert response.status_code == status
    storage.assert_not_called()


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("broken_link", ["school_id", "school", "institute"])
async def test_admin_missing_tenant_links_fail_closed(isolated, storage, route, broken_link):
    if broken_link == "school_id":
        isolated.db["newsavedDocs"].documents[0].pop("school_id")
    elif broken_link == "school":
        isolated.db["schoolDetails"].documents.clear()
    else:
        isolated.db["instituteDetails"].documents.clear()
    response = await isolated.client.get(f"/{route}/{isolated.exam}", headers={"x-test-user": "admin"})
    assert response.status_code == 404
    storage.assert_not_called()


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("field,user", [("faculty_id", "owner"), ("school_id", "admin")])
async def test_legacy_string_references(isolated, storage, route, field, user):
    exam = isolated.db["newsavedDocs"].documents[0]
    exam[field] = str(exam[field])
    response = await isolated.client.get(f"/{route}/{isolated.exam}", headers={"x-test-user": user})
    assert response.status_code == 200, response.text


async def test_authorized_empty_zip_keeps_not_found_behavior(isolated, storage):
    isolated.db["answerDetails"].documents.clear()
    response = await isolated.client.get(f"/download-folder/{isolated.exam}", headers={"x-test-user": "owner"})
    assert response.status_code == 404
    assert response.json()["detail"] == "No answer scripts found in this folder"
    storage.assert_not_called()
