from unittest.mock import patch

from bson import ObjectId

from tests.test_security_fixes import _seed_and_login_user

_GENERATE_NOTES_PATCH = "app.api.routers.pomodoro.generate_notes"
_GENERATE_TEST_PATCH = "app.api.routers.pomodoro.generate_test"
_UPLOAD_PATCH = "app.api.routers.pomodoro.upload_file_to_imagekit"
_EXTRACT_TEXT_PATCH = "app.api.routers.pomodoro.extract_uploaded_document_text"
_EXTRACT_SECTION_PATCH = "app.api.routers.pomodoro.extract_and_section"


async def _learner(client_factory, test_db):
    return await _seed_and_login_user(test_db, client_factory, role=7, name="Pomodoro Learner")


async def test_ai_driven_generate_requires_auth(client):
    resp = await client.post("/api/pomodoro/ai-driven/generate", json={"prompt": "study biology"})
    assert resp.status_code == 401


async def test_ai_driven_generate_requires_prompt(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post("/api/pomodoro/ai-driven/generate", json={"prompt": "  "})
    assert resp.status_code == 400


async def test_ai_driven_generate_rejects_non_int_config(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post("/api/pomodoro/ai-driven/generate", json={
        "prompt": "study biology", "total_study_time": "not-a-number",
    })
    assert resp.status_code == 422


async def test_ai_driven_generate_queues_job(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    with patch(_GENERATE_NOTES_PATCH, return_value=([], {})):
        resp = await learner.post("/api/pomodoro/ai-driven/generate", json={"prompt": "study biology"})
    assert resp.status_code == 202
    assert "job_id" in resp.json()


async def test_ai_assisted_upload_rejects_non_int_form_field(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post(
        "/api/pomodoro/ai-assisted/upload",
        data={"total_study_time": "not-a-number"},
        files={"file": ("notes.pdf", b"%PDF-fake", "application/pdf")},
    )
    assert resp.status_code == 422


async def test_ai_assisted_upload_queues_job(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    with patch(_UPLOAD_PATCH, return_value={"url": "https://cdn.test/x.pdf", "file_id": "f1"}), \
         patch(_EXTRACT_TEXT_PATCH, return_value=("extracted", {})), \
         patch(_EXTRACT_SECTION_PATCH, return_value=([], {})):
        resp = await learner.post(
            "/api/pomodoro/ai-assisted/upload",
            data={"total_study_time": "45"},
            files={"file": ("notes.pdf", b"%PDF-fake", "application/pdf")},
        )
    assert resp.status_code == 202
    assert "job_id" in resp.json()


async def test_custom_create_requires_auth(client):
    resp = await client.post("/api/pomodoro/custom/create", json={})
    assert resp.status_code == 401


async def test_custom_create_rejects_non_int_field(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post("/api/pomodoro/custom/create", json={"study_time_mins": "not-a-number"})
    assert resp.status_code == 422


async def test_custom_create_success(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post("/api/pomodoro/custom/create", json={"study_time_mins": 30, "num_sessions": 2})
    assert resp.status_code == 201
    assert "session_id" in resp.json()


async def test_submit_test_invalid_session_id(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post("/api/pomodoro/session/not-valid/submit-test", json={"answers": []})
    assert resp.status_code == 400


async def test_submit_test_rejects_non_list_answers(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post(f"/api/pomodoro/session/{ObjectId()}/submit-test", json={"answers": "not-a-list"})
    assert resp.status_code == 422


async def test_complete_session_rejects_invalid_status(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.patch(f"/api/pomodoro/session/{ObjectId()}/complete", json={"status": "banana"})
    assert resp.status_code == 422


async def test_complete_session_not_found(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.patch(f"/api/pomodoro/session/{ObjectId()}/complete", json={"status": "completed"})
    assert resp.status_code == 404


async def test_complete_session_success(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    created = await learner.post("/api/pomodoro/custom/create", json={"study_time_mins": 25})
    session_id = created.json()["session_id"]

    resp = await learner.patch(f"/api/pomodoro/session/{session_id}/complete", json={
        "status": "interrupted", "total_focused_mins": 12,
    })
    assert resp.status_code == 200
    assert "interrupted" in resp.json()["message"]
