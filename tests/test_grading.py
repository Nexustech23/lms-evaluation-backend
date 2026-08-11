from bson import ObjectId

from tests.conftest import login, register
from tests.test_security_fixes import PASSWORD, _register_institute_admin


async def _faculty_client(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Grading Institute")
    faculty_email = "faculty-grading@test.local"
    await register(
        institute, role="faculty", fullName="Grading Faculty", email=faculty_email,
        password=PASSWORD, school_id=str(ObjectId()),
    )
    fc = await client_factory()
    await login(fc, faculty_email, PASSWORD)
    return fc


async def test_evaluate_answer_script_requires_auth(client):
    resp = await client.post("/evaluate-answer-script", json={
        "folderId": str(ObjectId()), "answerId": str(ObjectId()),
    })
    assert resp.status_code == 401


async def test_evaluate_answer_script_rejects_missing_fields(superadmin_client, client_factory):
    fc = await _faculty_client(superadmin_client, client_factory)
    resp = await fc.post("/evaluate-answer-script", json={"folderId": str(ObjectId())})
    assert resp.status_code == 422


async def test_evaluate_answer_script_rejects_blank_ids(superadmin_client, client_factory):
    fc = await _faculty_client(superadmin_client, client_factory)
    resp = await fc.post("/evaluate-answer-script", json={"folderId": "  ", "answerId": str(ObjectId())})
    assert resp.status_code == 422


async def test_evaluate_answer_script_accepts_and_queues_job(superadmin_client, client_factory):
    fc = await _faculty_client(superadmin_client, client_factory)
    resp = await fc.post("/evaluate-answer-script", json={
        "folderId": str(ObjectId()), "answerId": str(ObjectId()), "generateTranscriptPdf": True,
    })
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "processing"
    assert body["generate_transcript_pdf"] is True
    assert "job_id" in body


async def test_evaluation_status_not_found(superadmin_client):
    resp = await superadmin_client.get(f"/evaluate-answer-script/status/{ObjectId()}")
    assert resp.status_code == 404
