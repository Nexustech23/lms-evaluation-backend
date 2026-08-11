from unittest.mock import patch

from bson import ObjectId

from tests.conftest import login, register
from tests.test_security_fixes import PASSWORD, _register_institute_admin

_GENERATE_TEXT_PATCH = "app.api.routers.question_paper.generate_text"


async def _faculty_client(superadmin_client, client_factory, test_db):
    institute = await _register_institute_admin(superadmin_client, client_factory, "QP Institute")
    faculty_email = "faculty-qp@test.local"
    await register(
        institute, role="faculty", fullName="QP Faculty", email=faculty_email,
        password=PASSWORD, school_id=str(ObjectId()),
    )
    fc = await client_factory()
    await login(fc, faculty_email, PASSWORD)
    return fc


async def test_generate_ai_requires_auth(client):
    resp = await client.post("/question-paper/generate-ai", data={"prompt": "algebra"})
    assert resp.status_code == 401


async def test_generate_ai_rejects_non_int_total_marks(superadmin_client, client_factory, test_db):
    fc = await _faculty_client(superadmin_client, client_factory, test_db)
    resp = await fc.post("/question-paper/generate-ai", data={"prompt": "algebra", "totalMarks": "not-a-number"})
    assert resp.status_code == 422


async def test_generate_ai_requires_prompt_or_file(superadmin_client, client_factory, test_db):
    fc = await _faculty_client(superadmin_client, client_factory, test_db)
    resp = await fc.post("/question-paper/generate-ai", data={"prompt": ""})
    assert resp.json()["error"] == "Provide either a 'prompt' or a 'questionBank' file."


async def test_generate_ai_queues_job(superadmin_client, client_factory, test_db):
    fc = await _faculty_client(superadmin_client, client_factory, test_db)
    with patch(_GENERATE_TEXT_PATCH, return_value=("<html>QP</html>", {"total_tokens": 10})):
        resp = await fc.post("/question-paper/generate-ai", data={"prompt": "algebra"})
    assert resp.status_code == 202
    assert "jobId" in resp.json()


async def test_render_diagram_requires_spec(superadmin_client):
    resp = await superadmin_client.post("/question-paper/render-diagram", json={})
    assert resp.status_code == 422


async def test_render_diagram_success(superadmin_client):
    resp = await superadmin_client.post("/question-paper/render-diagram", json={"spec": {"type": "generic"}})
    assert resp.status_code == 200
    assert resp.json()["image"].startswith("data:image/png;base64,")


async def test_save_question_paper_requires_subject_id(superadmin_client, client_factory, test_db):
    fc = await _faculty_client(superadmin_client, client_factory, test_db)
    resp = await fc.post("/question-paper/save", json={"editorContent": "<p>hi</p>"})
    assert resp.status_code == 422


async def test_save_question_paper_rejects_invalid_subject_id(superadmin_client, client_factory, test_db):
    fc = await _faculty_client(superadmin_client, client_factory, test_db)
    resp = await fc.post("/question-paper/save", json={"subjectId": "not-an-object-id"})
    assert resp.status_code == 400


async def test_list_question_papers_requires_auth(client):
    resp = await client.get("/question-paper")
    assert resp.status_code == 401


async def test_get_question_paper_not_found(superadmin_client, client_factory, test_db):
    fc = await _faculty_client(superadmin_client, client_factory, test_db)
    resp = await fc.get(f"/question-paper/{ObjectId()}")
    assert resp.status_code == 404


async def test_update_question_paper_not_found(superadmin_client, client_factory, test_db):
    fc = await _faculty_client(superadmin_client, client_factory, test_db)
    resp = await fc.put(f"/question-paper/{ObjectId()}", json={"subjectName": "New Name"})
    assert resp.status_code == 404


async def test_delete_question_paper_not_found(superadmin_client, client_factory, test_db):
    fc = await _faculty_client(superadmin_client, client_factory, test_db)
    resp = await fc.delete(f"/question-paper/{ObjectId()}")
    assert resp.status_code == 404
