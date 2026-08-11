from unittest.mock import patch

from bson import ObjectId

from tests.test_security_fixes import _seed_and_login_user

# The router's create-test endpoint fires a real AI-generation background
# task synchronously within the request/response cycle under httpx's
# ASGITransport (no ASGI lifespan means nothing schedules it separately) —
# tests must not depend on a live Claude/Gemini call, so it's patched out
# wherever a test creates a mock test.
_PATCH_TARGET = "app.api.routers.mock_tests.generate_mock_test_questions"


async def _learner_client(client_factory, test_db):
    return await _seed_and_login_user(test_db, client_factory, role=7, name="Mock Test Learner")


async def _create_test(learner, **payload):
    with patch(_PATCH_TARGET, return_value=[]):
        return await learner.post("/mock-tests", json=payload)


async def test_create_mock_test_requires_auth(client):
    resp = await client.post("/mock-tests", json={"subjectName": "Math"})
    assert resp.status_code == 401


async def test_create_mock_test_requires_subject(client_factory, test_db):
    learner = await _learner_client(client_factory, test_db)
    resp = await learner.post("/mock-tests", json={})
    assert resp.status_code == 400


async def test_create_mock_test_rejects_non_int_question_count(client_factory, test_db):
    learner = await _learner_client(client_factory, test_db)
    resp = await learner.post("/mock-tests", json={"subjectName": "Math", "questionCount": "lots"})
    assert resp.status_code == 422


async def test_create_and_list_mock_test(client_factory, test_db):
    learner = await _learner_client(client_factory, test_db)
    created = await _create_test(learner, subjectName="Math", questionCount=5)
    assert created.status_code == 200
    test_id = created.json()["testId"]

    listed = await learner.get("/mock-tests")
    assert listed.status_code == 200
    assert len(listed.json()["tests"]) == 1
    assert listed.json()["tests"][0]["_id"] == test_id


async def test_get_mock_test_not_found(client_factory, test_db):
    learner = await _learner_client(client_factory, test_db)
    resp = await learner.get(f"/mock-tests/{ObjectId()}")
    assert resp.status_code == 404


async def test_submit_test_rejects_non_dict_answers(client_factory, test_db):
    learner = await _learner_client(client_factory, test_db)
    created = await _create_test(learner, subjectName="Math")
    test_id = created.json()["testId"]

    resp = await learner.post(f"/mock-tests/{test_id}/submit", json={"answers": ["not", "a", "dict"]})
    assert resp.status_code == 422


async def test_submit_test_scores_answers(client_factory, test_db):
    learner = await _learner_client(client_factory, test_db)
    created = await _create_test(learner, subjectName="Math", questionCount=2)
    test_id = created.json()["testId"]

    # Overwrite with known questions (the create call was patched to return
    # none, so the questions we score against are set here directly).
    await test_db["mockTests"].update_one(
        {"_id": ObjectId(test_id)},
        {"$set": {"questions": [
            {"_id": "q1", "correct_answer": "A", "marks": 1},
            {"_id": "q2", "correct_answer": "B", "marks": 1},
        ]}},
    )

    resp = await learner.post(f"/mock-tests/{test_id}/submit", json={"answers": {"q1": "A", "q2": "C"}})
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["correct"] == 1
    assert result["wrong"] == 1
    assert result["skipped"] == 0
    assert result["scored"] == 1

    review = await learner.get(f"/mock-tests/{test_id}/review")
    assert review.status_code == 200
