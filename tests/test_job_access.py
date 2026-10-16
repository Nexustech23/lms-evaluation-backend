"""Job privacy regressions with in-memory Redis and mocked external services."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from bson import ObjectId
from fastapi import FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_identity
from app.api.routers import grading, question_paper as qp
from app.db.mongodb import get_database
from app.models.user import FACULTY, INSTITUTE, INSTITUTE_STUDENT, SUPERADMIN
from app.services import job_store


class MemoryRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, ttl, value):
        self.values[key] = value


@pytest_asyncio.fixture
async def jobs(monkeypatch):
    redis = MemoryRedis()
    monkeypatch.setattr(job_store, "get_redis", lambda: redis)
    owner = str(ObjectId())
    identities = {
        "owner": {"user_id": owner, "role": FACULTY},
        "faculty": {"user_id": str(ObjectId()), "role": FACULTY},
        "student": {"user_id": str(ObjectId()), "role": INSTITUTE_STUDENT},
        "admin": {"user_id": str(ObjectId()), "role": INSTITUTE},
        "superadmin": {"user_id": str(ObjectId()), "role": SUPERADMIN},
    }
    app = FastAPI()
    app.include_router(grading.router)
    app.include_router(qp.router)

    async def identity(request: Request):
        name = request.headers.get("x-test-user")
        if name not in identities:
            raise HTTPException(401, "Login required")
        return identities[name]

    app.dependency_overrides[get_current_identity] = identity
    app.dependency_overrides[get_database] = lambda: {}
    app.dependency_overrides[grading.bulk_grading_rate_limit] = lambda: None
    app.dependency_overrides[qp.ai_rate_limit] = lambda: None
    faculty_id = ObjectId()
    monkeypatch.setattr(grading, "get_current_user_and_faculty_details",
                        AsyncMock(return_value=({"_id": ObjectId(owner)}, faculty_id, None)))
    monkeypatch.setattr(qp, "_require_faculty",
                        AsyncMock(return_value=({"_id": ObjectId(owner)}, faculty_id)))
    monkeypatch.setattr(qp, "_get_institute_name", AsyncMock(return_value="Test Institute"))
    budget = {"allowed": True, "warnings": []}
    for module in (grading, qp):
        monkeypatch.setattr(module, "check_institute_token_budget", AsyncMock(return_value=budget))
        monkeypatch.setattr(module, "enqueue", AsyncMock())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://isolated") as client:
        yield SimpleNamespace(client=client, owner=owner, redis=redis, faculty=faculty_id)


ENDPOINTS = [(grading.EVAL_JOB_PREFIX, "/evaluate-answer-script/status/"),
             (qp.QP_JOB_PREFIX, "/question-paper/generate-ai/status/")]


@pytest.mark.parametrize("prefix,url", ENDPOINTS)
@pytest.mark.parametrize("state", ["processing", "done", "error"])
@pytest.mark.parametrize("user", [None, "faculty", "student", "admin", "superadmin"])
async def test_other_accounts_cannot_read_job_in_any_state(jobs, prefix, url, state, user):
    await job_store.set_job(prefix, "private", {
        "owner_user_id": jobs.owner, "status": state,
        "paperText": "SECRET PAPER", "result": {"private_marks": 8}, "error": "SECRET ERROR",
    })
    response = await jobs.client.get(url + "private", headers={"x-test-user": user} if user else {})
    assert response.status_code == (401 if user is None else 404)
    assert "SECRET" not in response.text and "private_marks" not in response.text


@pytest.mark.parametrize("prefix,url", ENDPOINTS)
@pytest.mark.parametrize("owner", [None, "", "different-owner"])
async def test_legacy_or_foreign_owner_jobs_fail_closed(jobs, prefix, url, owner):
    payload = {"status": "completed", "paperText": "SECRET"}
    if owner is not None:
        payload["owner_user_id"] = owner
    await job_store.set_job(prefix, "legacy", payload)
    response = await jobs.client.get(url + "legacy", headers={"x-test-user": "owner"})
    assert response.status_code == 404
    assert "SECRET" not in response.text


@pytest.mark.parametrize("prefix,url", ENDPOINTS)
async def test_missing_jobs_return_404(jobs, prefix, url):
    response = await jobs.client.get(url + "missing", headers={"x-test-user": "owner"})
    assert response.status_code == 404


@pytest.mark.parametrize("prefix,url", ENDPOINTS)
async def test_late_update_after_expiration_does_not_expose_ownerless_result(jobs, prefix, url):
    await job_store.set_job(prefix, "expired", {"owner_user_id": jobs.owner})
    jobs.redis.values.clear()
    await job_store.update_job(prefix, "expired", {"status": "completed", "paperText": "SECRET"})
    response = await jobs.client.get(url + "expired", headers={"x-test-user": "owner"})
    assert response.status_code == 404
    assert "SECRET" not in response.text


@pytest.mark.parametrize("prefix,url", ENDPOINTS)
async def test_owner_readable_after_progress_success_and_error_updates(jobs, prefix, url):
    await job_store.set_job(prefix, "mine", {"owner_user_id": jobs.owner, "status": "queued"})
    for state, expected in [("processing", 202 if prefix == qp.QP_JOB_PREFIX else 200),
                            ("completed" if prefix == qp.QP_JOB_PREFIX else "done", 200),
                            ("failed" if prefix == qp.QP_JOB_PREFIX else "error", 500 if prefix == qp.QP_JOB_PREFIX else 200)]:
        await job_store.update_job(prefix, "mine", {"status": state, "paperText": "My paper", "result": {"score": 8}})
        assert (await job_store.get_job(prefix, "mine"))["owner_user_id"] == jobs.owner
        response = await jobs.client.get(url + "mine", headers={"x-test-user": "owner"})
        assert response.status_code == expected, response.text
        assert response.json()["status"] == state


@pytest.mark.parametrize("kind", ["evaluation", "generation"])
async def test_creation_uses_authenticated_owner_not_client_input(jobs, kind):
    headers = {"x-test-user": "owner"}
    if kind == "evaluation":
        response = await jobs.client.post("/evaluate-answer-script", headers=headers, json={
            "folderId": str(ObjectId()), "answerId": str(ObjectId()), "owner_user_id": "forged",
        })
        job_id = response.json()["job_id"]
        prefix = grading.EVAL_JOB_PREFIX
    else:
        response = await jobs.client.post("/question-paper/generate-ai", headers=headers,
                                          data={"prompt": "Data science exam", "owner_user_id": "forged"})
        job_id = response.json()["jobId"]
        prefix = qp.QP_JOB_PREFIX
    assert response.status_code == 202, response.text
    assert (await job_store.get_job(prefix, job_id))["owner_user_id"] == jobs.owner


@pytest.mark.parametrize("failure", ["missing", "runtime", "exception"])
async def test_evaluation_worker_failure_preserves_owner(jobs, monkeypatch, failure):
    lookup = AsyncMock(return_value=None)
    if failure != "missing":
        lookup.side_effect = RuntimeError("worker error") if failure == "runtime" else ValueError("bad data")
    monkeypatch.setattr(grading, "get_database", lambda: {"newsavedDocs": SimpleNamespace(find_one=lookup)})
    await job_store.set_job(grading.EVAL_JOB_PREFIX, "worker", {"owner_user_id": jobs.owner})
    await grading._run_evaluation_job("worker", str(ObjectId()), str(ObjectId()), False, str(jobs.faculty))
    job = await job_store.get_job(grading.EVAL_JOB_PREFIX, "worker")
    assert job["owner_user_id"] == jobs.owner
    assert job["status"] == "error"


@pytest.mark.parametrize("fail", [False, True])
async def test_generation_worker_completion_and_failure_preserve_owner(jobs, monkeypatch, fail):
    monkeypatch.setattr(qp, "get_database", lambda: {})
    monkeypatch.setattr(qp, "resolve_institute_id_for_faculty", AsyncMock(return_value=None))
    monkeypatch.setattr(qp, "_build_claude_prompt", Mock(return_value="prompt"))
    generate = Mock(return_value=("My exam", {"input_tokens": 1, "output_tokens": 1}))
    if fail:
        generate.side_effect = RuntimeError("generation failed")
    monkeypatch.setattr(qp, "generate_text", generate)
    monkeypatch.setattr(qp, "increment_institute_claude_tokens", AsyncMock())
    monkeypatch.setattr(qp, "build_docx", Mock(return_value=b"docx"))
    monkeypatch.setattr(qp, "upload_file_to_imagekit", Mock(return_value={"url": "https://cdn.test/doc", "file_id": "file"}))
    await job_store.set_job(qp.QP_JOB_PREFIX, "worker", {"owner_user_id": jobs.owner})
    params = dict.fromkeys(["prompt", "institute_name", "department_name", "subject_name", "exam_type",
                           "semester", "academic_year", "duration"], "test")
    params.update(faculty_id=str(jobs.faculty), sections=[], total_marks=10)
    await qp._run_generation_job("worker", params, {})
    job = await job_store.get_job(qp.QP_JOB_PREFIX, "worker")
    assert job["owner_user_id"] == jobs.owner
    assert job["status"] == ("failed" if fail else "completed")
