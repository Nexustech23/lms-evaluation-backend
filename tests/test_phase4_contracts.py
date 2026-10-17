"""Final app-wiring, Redis-command and worker argument compatibility checks."""
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_identity
from app.services import job_store


@pytest.mark.parametrize('prefix,url', [
    ('job:', '/evaluate-answer-script/status/'),
    ('qp_job:', '/question-paper/generate-ai/status/'),
])
@pytest.mark.parametrize('owns_job', [True, False])
async def test_real_app_router_enforces_job_owner_without_external_services(app, fake_redis_server, prefix, url, owns_job):
    from app.api.routers.question_paper import QP_JOB_PREFIX
    # Use the actual generation namespace rather than assuming its spelling.
    if 'question-paper' in url:
        prefix = QP_JOB_PREFIX
    owner = str(ObjectId())
    identity = {'user_id': owner if owns_job else str(ObjectId()), 'role': 3}
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_current_identity] = lambda: identity
    await job_store.set_job(prefix, 'contract', {
        'owner_user_id': owner, 'status': 'processing', 'progress': 10,
    })
    try:
        # Lifespan intentionally not started: no MongoDB or real Redis connection.
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get(url + 'contract')
        assert response.status_code == ((202 if 'question-paper' in url else 200) if owns_job else 404)
        if owns_job:
            assert response.json()['status'] == 'processing'
        else:
            assert 'progress' not in response.json()
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize('version', [None, 'new-paper-version'])
async def test_extraction_worker_forwards_legacy_and_versioned_queue_arguments(monkeypatch, version):
    from app import worker
    from app.db import mongodb
    from app.services import gemini
    db = object()
    monkeypatch.setattr(mongodb, 'get_database', lambda: db)
    extract = AsyncMock()
    monkeypatch.setattr(gemini, 'extract_and_patch_question_paper_text', extract)
    exam = ObjectId()
    args = [None, str(exam), 'https://cdn.test/paper.pdf', 'faculty', 'paper.pdf', 'user']
    if version is not None:
        args.append(version)
    await worker.run_extract_question_paper_text(*args)
    extract.assert_awaited_once_with(db, exam, 'https://cdn.test/paper.pdf', 'faculty', 'paper.pdf', 'user', version)


@pytest.mark.parametrize('prefix', ['job:', 'qp_job:'])
async def test_job_store_redis_commands_preserve_owner_and_expiry(fake_redis_server, prefix):
    identity = {'user_id': str(ObjectId())}
    await job_store.set_job(prefix, 'redis-contract', {'owner_user_id': identity['user_id'], 'status': 'queued'})
    for state in ('processing', 'done', 'error'):
        await job_store.set_job(prefix, 'redis-contract', {'owner_user_id': identity['user_id'], 'status': 'queued'})
        await job_store.update_job(prefix, 'redis-contract', {'status': state})
        job = await job_store.get_owned_job(prefix, 'redis-contract', identity)
        assert job['owner_user_id'] == identity['user_id'] and job['status'] == state
        assert 0 < await fake_redis_server.ttl(prefix + 'redis-contract') <= job_store.JOB_TTL
