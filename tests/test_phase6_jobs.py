"""Concurrent job submission/state regressions; no paid or storage requests."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from bson import ObjectId

from app.api.routers import grading, question_paper as qp
from app.services import job_store
from app.services.job_submission import generation_field, release_submission
from tests.test_job_access import jobs
from tests.test_failed_ai_cost import accounting, Collection


async def submit(s):
    return await s.client.post('/evaluate-answer-script', headers={'x-test-user': 'owner'},
        json={'folderId': s.exam, 'answerId': s.answer})


async def test_concurrent_evaluation_requests_enqueue_once(jobs):
    responses = await asyncio.gather(*(submit(jobs) for _ in range(8)))
    assert all(r.status_code == 202 for r in responses)
    assert len({r.json()['job_id'] for r in responses}) == 1
    grading.enqueue.assert_awaited_once()


async def test_active_job_can_be_reattached_even_when_budget_exhausted(jobs):
    first = await submit(jobs)
    grading.check_institute_token_budget.return_value = {'allowed': False, 'message': 'limit', 'warnings': []}
    second = await submit(jobs)
    assert second.status_code == 202
    assert second.json()['job_id'] == first.json()['job_id']
    grading.enqueue.assert_awaited_once()


async def test_terminal_job_releases_guard_for_explicit_retry(jobs):
    first = await submit(jobs)
    await job_store.update_job(grading.EVAL_JOB_PREFIX, first.json()['job_id'], {'status': 'error'})
    second = await submit(jobs)
    assert second.status_code == 202
    assert second.json()['job_id'] != first.json()['job_id']
    assert grading.enqueue.await_count == 2


async def test_missing_status_does_not_blindly_restart_paid_job(jobs):
    await submit(jobs)
    await jobs.redis.flushall()
    response = await submit(jobs)
    assert response.status_code == 409
    grading.enqueue.assert_awaited_once()


async def test_reviewed_answer_is_protected_before_enqueue(jobs):
    jobs.db['answerDetails'].docs[0]['reviewed_by_professor'] = True
    response = await submit(jobs)
    assert response.status_code == 409
    grading.enqueue.assert_not_awaited()


async def test_wrong_folder_is_rejected_before_enqueue(jobs):
    response = await jobs.client.post('/evaluate-answer-script', headers={'x-test-user': 'owner'},
        json={'folderId': str(ObjectId()), 'answerId': jobs.answer})
    assert response.status_code == 400
    grading.enqueue.assert_not_awaited()


async def test_foreign_faculty_cannot_claim_answer_job(jobs):
    response = await jobs.client.post('/evaluate-answer-script', headers={'x-test-user': 'faculty'},
        json={'folderId': jobs.exam, 'answerId': jobs.answer})
    assert response.status_code == 404
    grading.enqueue.assert_not_awaited()


async def test_uncertain_dispatch_retry_reuses_existing_job(jobs):
    grading.enqueue.side_effect = RuntimeError('Queue result unavailable')
    first = await submit(jobs)
    assert first.status_code == 503
    second = await submit(jobs)
    assert second.status_code == 202
    assert first.json()['job_id'] == second.json()['job_id']
    grading.enqueue.assert_awaited_once()


async def test_budget_rejection_releases_guard_without_spend(jobs):
    grading.check_institute_token_budget.return_value = {'allowed': False, 'message': 'limit', 'warnings': []}
    assert (await submit(jobs)).status_code == 402
    assert 'active_evaluation' not in jobs.db['answerDetails'].docs[0]
    grading.enqueue.assert_not_awaited()


async def test_concurrent_identical_paper_requests_enqueue_once(jobs):
    async def paper():
        return await jobs.client.post('/question-paper/generate-ai', headers={'x-test-user': 'owner'},
                                      data={'prompt': 'Data science exam'})
    responses = await asyncio.gather(*(paper() for _ in range(6)))
    assert all(r.status_code == 202 for r in responses)
    assert len({r.json()['jobId'] for r in responses}) == 1
    qp.enqueue.assert_awaited_once()


def test_paper_fingerprint_includes_source_bytes_and_parameters():
    first = generation_field({'subject': 'DS'}, {'questionBank': b'first'})
    assert first != generation_field({'subject': 'DS'}, {'questionBank': b'second'})
    assert first != generation_field({'subject': 'CS'}, {'questionBank': b'first'})


async def test_old_worker_cannot_release_new_job_guard(jobs):
    first = await submit(jobs)
    await release_submission(jobs.db, 'answerDetails', {'_id': ObjectId(jobs.answer)}, 'active_evaluation', 'other-job')
    assert jobs.db['answerDetails'].docs[0]['active_evaluation']['job_id'] == first.json()['job_id']


async def test_duplicate_execution_delivery_has_one_winner(fake_redis_server):
    await job_store.set_job('job:', 'once', {'owner_user_id': 'owner', 'status': 'queued'})
    claims = await asyncio.gather(*(job_store.claim_job_execution('job:', 'once') for _ in range(10)))
    assert sum(claims) == 1


async def test_progress_updates_are_atomic_and_owner_immutable(fake_redis_server):
    await job_store.set_job('job:', 'merge', {'owner_user_id': 'owner', 'status': 'queued'})
    await asyncio.gather(job_store.update_job('job:', 'merge', {'progress': 10}),
                         job_store.update_job('job:', 'merge', {'step': 'OCR', 'owner_user_id': 'forged'}))
    job = await job_store.get_job('job:', 'merge')
    assert job['progress'] == 10 and job['step'] == 'OCR' and job['owner_user_id'] == 'owner'


@pytest.mark.parametrize('terminal', ['done', 'completed', 'error', 'failed'])
async def test_terminal_state_cannot_be_regressed(fake_redis_server, terminal):
    await job_store.set_job('job:', 'terminal', {'owner_user_id': 'owner', 'status': terminal})
    await job_store.update_job('job:', 'terminal', {'status': 'processing', 'result': 'late'})
    assert (await job_store.get_job('job:', 'terminal'))['status'] == terminal
    assert not await job_store.claim_job_execution('job:', 'terminal')


async def test_expired_job_is_not_recreated(fake_redis_server):
    await job_store.update_job('job:', 'expired', {'status': 'done', 'result': 'private'})
    assert await job_store.get_job('job:', 'expired') is None


async def test_ownerless_job_cannot_start_paid_execution(fake_redis_server):
    await job_store.set_job('job:', 'legacy', {'status': 'queued'})
    assert not await job_store.claim_job_execution('job:', 'legacy')


async def test_duplicate_generation_delivery_runs_provider_once(jobs, monkeypatch):
    jobs.db['aiUsageEvents'] = Collection()
    monkeypatch.setattr(qp, 'get_database', lambda: jobs.db)
    monkeypatch.setattr(qp, '_build_claude_prompt', Mock(return_value='prompt'))
    provider = Mock(return_value=('Paper', {'input_tokens': 1, 'output_tokens': 1}))
    monkeypatch.setattr(qp, 'generate_text', provider)
    monkeypatch.setattr(qp, 'build_docx', Mock(return_value=b'docx'))
    monkeypatch.setattr(qp, 'upload_file_to_imagekit', Mock(return_value={'url': 'https://test/paper', 'file_id': 'file'}))
    params = dict.fromkeys(['prompt', 'institute_name', 'department_name', 'subject_name', 'exam_type',
                           'semester', 'academic_year', 'duration'], 'demo')
    params.update(faculty_id=str(jobs.faculty), user_id=jobs.owner, sections=[], total_marks=10)
    await job_store.set_job(qp.QP_JOB_PREFIX, 'once', {'owner_user_id': jobs.owner, 'status': 'queued'})
    await asyncio.gather(*(qp._run_generation_job('once', params, {}) for _ in range(3)))
    provider.assert_called_once()
    assert (await job_store.get_job(qp.QP_JOB_PREFIX, 'once'))['status'] == 'completed'


@pytest.mark.parametrize('change', ['reviewed', 'deleted'])
async def test_late_ai_save_keeps_reviewed_marks_and_cleans_new_artifact(accounting, monkeypatch, change):
    s = accounting
    monkeypatch.setattr(grading, 'get_database', lambda: s.db)
    monkeypatch.setattr(grading, 'update_job', AsyncMock())
    monkeypatch.setattr(grading, '_resolve_institute_id', AsyncMock(return_value=s.institute))
    monkeypatch.setattr(grading, '_download_pdf', Mock(return_value=b'pdf'))
    monkeypatch.setattr(grading, 'ensure_mcq_answer_key', AsyncMock(return_value={}))
    monkeypatch.setattr(grading, 'extract_answer_text_with_gemini', Mock(return_value=(
        'answer', {'prompt_tokens': 1, 'candidate_tokens': 1, 'total_tokens': 2})))
    monkeypatch.setattr(grading, 'grade_with_claude', Mock(return_value=('JSON', {'input_tokens': 1, 'output_tokens': 1})))
    monkeypatch.setattr(grading, 'safe_json_parse', Mock(return_value={'questionwise_marking': []}))
    monkeypatch.setattr(grading, 'generate_evaluation_report_html', Mock(return_value='html'))
    monkeypatch.setattr(grading, 'render_html_to_pdf', Mock(return_value=b'pdf'))
    def upload(*args, **kwargs):
        if change == 'reviewed':
            s.db['answerDetails'].docs[0].update(reviewed_by_professor=True, total_final_marks=0)
        else:
            s.db['answerDetails'].docs.clear()
        return {'url': 'https://test/new-report', 'file_id': 'new-report'}
    monkeypatch.setattr(grading, 'upload_file_to_imagekit', Mock(side_effect=upload))
    await grading._run_evaluation_job('late', s.exam, s.answer, False, s.faculty, s.user)
    assert grading.update_job.await_args.args[2]['status'] == 'error'
    grading.delete_imagekit_file.assert_called_once_with('new-report')
    if change == 'reviewed':
        assert s.db['answerDetails'].docs[0]['total_final_marks'] == 0
        assert s.db['answerDetails'].docs[0]['reviewed_by_professor'] is True


@pytest.mark.parametrize('kind', ['evaluation', 'generation'])
async def test_interrupted_worker_retains_submission_guard(jobs, monkeypatch, kind):
    if kind == 'evaluation':
        response = await submit(jobs)
        job_id = response.json()['job_id']
        monkeypatch.setattr(grading, 'get_database', lambda: jobs.db)
        monkeypatch.setattr(jobs.db['newsavedDocs'], 'find_one', AsyncMock(side_effect=asyncio.CancelledError()))
        with pytest.raises(asyncio.CancelledError):
            await grading._run_evaluation_job(job_id, jobs.exam, jobs.answer, False, str(jobs.faculty), jobs.owner)
        assert jobs.db['answerDetails'].docs[0]['active_evaluation']['job_id'] == job_id
        assert (await job_store.get_job(grading.EVAL_JOB_PREFIX, job_id))['status'] == 'interrupted'
    else:
        response = await jobs.client.post('/question-paper/generate-ai', headers={'x-test-user': 'owner'},
                                          data={'prompt': 'Exam'})
        job_id = response.json()['jobId']
        params = qp.enqueue.await_args.args[2]
        monkeypatch.setattr(qp, 'get_database', lambda: jobs.db)
        monkeypatch.setattr(qp, '_build_claude_prompt', Mock(side_effect=asyncio.CancelledError()))
        with pytest.raises(asyncio.CancelledError):
            await qp._run_generation_job(job_id, params, {})
        assert jobs.db['users'].docs[0]['active_paper_jobs']
        assert (await job_store.get_job(qp.QP_JOB_PREFIX, job_id))['status'] == 'interrupted'


async def test_running_generation_reattaches_after_budget_is_exhausted(jobs):
    async def paper():
        return await jobs.client.post('/question-paper/generate-ai', headers={'x-test-user': 'owner'}, data={'prompt': 'Exam'})
    first = await paper()
    qp.check_institute_token_budget.return_value = {'allowed': False, 'warnings': [], 'message': 'limit'}
    second = await paper()
    assert second.status_code == 202
    assert first.json()['jobId'] == second.json()['jobId']
    qp.enqueue.assert_awaited_once()
