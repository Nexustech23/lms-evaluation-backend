from unittest.mock import patch

from bson import ObjectId

from tests.test_security_fixes import _seed_and_login_user

_GEMINI_JSON_PATCH = "app.api.routers.roadmap.generate_gemini_json"
_CURRICULUM_PATCH = "app.api.routers.roadmap.generate_curriculum"


async def _learner(client_factory, test_db):
    return await _seed_and_login_user(test_db, client_factory, role=7, name="Roadmap Learner")


async def test_assess_requires_auth(client):
    resp = await client.post("/api/self-learner/roadmap/assess", json={"subject": "Math"})
    assert resp.status_code == 401


async def test_assess_rejects_blank_subject(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post("/api/self-learner/roadmap/assess", json={"subject": "  "})
    assert resp.status_code == 422


async def test_assess_rejects_too_long_subject(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post("/api/self-learner/roadmap/assess", json={"subject": "x" * 201})
    assert resp.status_code == 422


async def test_assess_success(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    with patch(_GEMINI_JSON_PATCH, return_value=([{"question": "2+2?"}], {}, False)):
        resp = await learner.post("/api/self-learner/roadmap/assess", json={"subject": "Math"})
    assert resp.status_code == 200
    assert resp.json()["questions"] == [{"question": "2+2?"}]


async def test_create_roadmap_requires_subject(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post("/api/self-learner/roadmap", json={})
    assert resp.status_code == 422


async def test_create_roadmap_rejects_too_long_goal(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post(
        "/api/self-learner/roadmap", json={"subject": "Math", "goal": "x" * 501}
    )
    assert resp.status_code == 422


async def test_create_roadmap_queues_job(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    with patch(_CURRICULUM_PATCH, return_value=({"levels": []}, {}, False)):
        resp = await learner.post("/api/self-learner/roadmap", json={"subject": "Math"})
    assert resp.status_code == 202
    assert resp.json()["status"] == "processing"


async def test_update_subtopic_requires_key(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.patch(f"/api/self-learner/roadmap/{ObjectId()}/subtopic", json={})
    assert resp.status_code == 422


async def test_update_subtopic_invalid_roadmap_id(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.patch(
        "/api/self-learner/roadmap/not-valid/subtopic", json={"subtopic_key": "1-0"}
    )
    assert resp.status_code == 400


async def test_update_subtopic_not_found(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.patch(
        f"/api/self-learner/roadmap/{ObjectId()}/subtopic", json={"subtopic_key": "1-0"}
    )
    assert resp.status_code == 404


async def test_submit_quiz_invalid_roadmap_id(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post(
        "/api/self-learner/roadmap/not-valid/quiz/submit", json={"level": 1, "answers": {}}
    )
    assert resp.status_code == 400


async def test_submit_quiz_rejects_non_int_level(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post(
        f"/api/self-learner/roadmap/{ObjectId()}/quiz/submit", json={"level": "not-a-number", "answers": {}}
    )
    assert resp.status_code == 422


async def test_submit_quiz_not_found(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post(
        f"/api/self-learner/roadmap/{ObjectId()}/quiz/submit", json={"level": 1, "answers": {}}
    )
    assert resp.status_code == 404


async def test_get_roadmaps_empty(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.get("/api/self-learner/roadmap")
    assert resp.status_code == 200
    assert resp.json() == []
