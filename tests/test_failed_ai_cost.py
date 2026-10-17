"""Failure accounting checks: isolated DB, no billable provider calls."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from bson import ObjectId

from app.services.faculty_ai_usage import tracked_faculty_call
from app.services import grading as grading_service
from app.api.routers import grading, question_paper as qp
from app.utils.token_usage import save_grading_tokens_to_institute


def get(doc, key):
    for part in key.split('.'):
        doc = doc.get(part) if isinstance(doc, dict) else None
    return doc


class Collection:
    def __init__(self, docs=()):
        self.docs = deepcopy(list(docs))
        self.writes = []

    def matching(self, doc, query):
        for key, value in query.items():
            actual = get(doc, key)
            if isinstance(value, dict) and "$exists" in value:
                if (actual is not None) != value["$exists"]:
                    return False
            elif isinstance(value, dict) and "$ne" in value:
                if actual == value["$ne"]:
                    return False
            elif actual != value:
                return False
        return True

    async def find_one(self, query, *args):
        return deepcopy(next((d for d in self.docs if self.matching(d, query)), None))

    def find(self, query):
        async def rows():
            for doc in list(self.docs):
                if self.matching(doc, query):
                    yield deepcopy(doc)
        return rows()

    async def insert_one(self, doc):
        doc = deepcopy(doc)
        doc.setdefault('_id', ObjectId())
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc['_id'])

    async def update_one(self, query, changes, upsert=False):
        self.writes.append(deepcopy(changes))
        doc = next((d for d in self.docs if self.matching(d, query)), None)
        inserted = doc is None
        if doc is None and upsert:
            doc = deepcopy(query)
            self.docs.append(doc)
        if doc is None:
            return SimpleNamespace(matched_count=0)
        for operator, fields in changes.items():
            if operator == '$setOnInsert' and not inserted:
                continue
            for dotted, val in fields.items():
                target = doc
                parts = dotted.split('.')
                for part in parts[:-1]:
                    target = target.setdefault(part, {})
                if operator == '$inc':
                    target[parts[-1]] = target.get(parts[-1], 0) + val
                elif operator == '$max':
                    target[parts[-1]] = max(target.get(parts[-1], val), val)
                elif operator == '$unset':
                    target.pop(parts[-1], None)
                else:
                    target[parts[-1]] = deepcopy(val)
        return SimpleNamespace(matched_count=1, modified_count=1)


@pytest.fixture
def accounting(monkeypatch):
    # These tests isolate accounting; execution claiming is covered separately.
    monkeypatch.setattr(grading, 'claim_job_execution', AsyncMock(return_value=True))
    monkeypatch.setattr(qp, 'claim_job_execution', AsyncMock(return_value=True))
    monkeypatch.setattr(grading, 'delete_imagekit_file', Mock())
    user, faculty, institute, exam, answer = [ObjectId() for _ in range(5)]
    db = {
        'users': Collection([{'_id': user}]),
        'facultyDetails': Collection([{'_id': faculty, 'user_id': user, 'institute_id': institute}]),
        'instituteDetails': Collection([{'_id': institute}]),
        'aiUsageEvents': Collection(),
        'newsavedDocs': Collection([{'_id': exam, 'faculty_id': faculty, 'question_paper': {'version': 'A', 'text': 'Question'}}]),
        'answerDetails': Collection([{'_id': answer, 'exam_id': exam, 'answer_script_url': 'https://cdn.test/student.pdf'}]),
        'evaluationDetails': Collection([{'exam_id': exam, 'totalMarks': 10, 'questionEvaluationDetails': [{'maxMarks': 10}]}]),
    }
    return SimpleNamespace(db=db, user=str(user), faculty=str(faculty), institute=institute, exam=str(exam), answer=str(answer))


def call(s, function, provider='claude', **kwargs):
    return tracked_faculty_call(s.db, function, faculty_id=s.faculty, user_id=s.user,
                                provider=provider, model='claude-sonnet-4-6' if provider == 'claude' else 'gemini-2.5-flash',
                                feature='qp_generation', job_id='job', **kwargs)


@pytest.mark.parametrize('provider,usage', [
    ('claude', {'input_tokens': 100, 'output_tokens': 50}),
    ('gemini', {'prompt_tokens': 100, 'candidate_tokens': 20, 'total_tokens': 150}),
])
async def test_usage_and_counters_survive_later_failure(accounting, provider, usage):
    s = accounting
    result, _ = await call(s, Mock(return_value=('AI response', usage)), provider)
    assert result == 'AI response'
    # A downstream PDF/JSON/storage error does not roll back already earned usage.
    with pytest.raises(RuntimeError):
        raise RuntimeError('Report upload failed')
    event = s.db['aiUsageEvents'].docs[0]
    assert event['usage_status'] == 'known' and event['cost_usd'] > 0
    assert event['total_tokens'] == 150
    assert get(s.db['instituteDetails'].docs[0], f'token_usage.{provider}.total_tokens') == 150
    assert len(s.db['aiUsageEvents'].docs) == 1


async def test_attempt_exists_before_provider_runs(accounting):
    def provider():
        event = accounting.db['aiUsageEvents'].docs[0]
        assert event['call_status'] == 'started'
        assert event['cost_usd'] is None
        return 'result', {'input_tokens': 1, 'output_tokens': 2}
    await call(accounting, provider)
    assert accounting.db['aiUsageEvents'].docs[0]['call_status'] == 'completed'


async def test_provider_error_keeps_unknown_not_zero(accounting):
    with pytest.raises(RuntimeError, match='timeout'):
        await call(accounting, Mock(side_effect=RuntimeError('timeout')))
    event = accounting.db['aiUsageEvents'].docs[0]
    assert event['call_status'] == 'failed' and event['usage_status'] == 'unknown'
    assert event['cost_usd'] is None and event['total_tokens'] is None
    assert 'token_usage' not in accounting.db['instituteDetails'].docs[0]


async def test_caught_grading_error_does_not_look_like_free_call(accounting, monkeypatch):
    monkeypatch.setattr(grading_service, 'generate_text', Mock(side_effect=RuntimeError('provider failed')))
    function = lambda: grading_service.grade_with_claude('question', 'answer', [{'maxMarks': 10}])
    await call(accounting, function)
    assert accounting.db['aiUsageEvents'].docs[0]['usage_status'] == 'unknown'
    assert accounting.db['aiUsageEvents'].docs[0]['cost_usd'] is None


async def test_parallel_success_is_recorded_even_when_sibling_fails(accounting):
    results = await asyncio.gather(
        call(accounting, Mock(return_value=('success', {'input_tokens': 20, 'output_tokens': 10}))),
        call(accounting, Mock(side_effect=RuntimeError('failed'))), return_exceptions=True,
    )
    assert isinstance(results[1], RuntimeError)
    events = accounting.db['aiUsageEvents'].docs
    assert sorted(e['usage_status'] for e in events) == ['known', 'unknown']
    assert get(accounting.db['instituteDetails'].docs[0], 'token_usage.claude.total_tokens') == 30


async def test_completed_evaluation_does_not_double_count_tokens(accounting):
    await call(accounting, Mock(return_value=('success', {'input_tokens': 20, 'output_tokens': 10})))
    await save_grading_tokens_to_institute(accounting.db, accounting.institute, {}, tokens_already_recorded=True)
    institute = accounting.db['instituteDetails'].docs[0]
    assert get(institute, 'token_usage.claude.total_tokens') == 30
    assert get(institute, 'token_usage.claude.call_count') == 1
    assert get(institute, 'token_usage.evaluation_count') == 1


async def test_legacy_worker_resolves_user_from_faculty(accounting):
    await tracked_faculty_call(accounting.db, Mock(return_value=('ok', {'input_tokens': 10, 'output_tokens': 1})),
                               faculty_id=accounting.faculty, user_id=None, provider='claude',
                               model='claude-sonnet-4-6', feature='qp_generation')
    assert str(accounting.db['aiUsageEvents'].docs[0]['user_id']) == accounting.user


async def test_failed_initial_attempt_insert_prevents_untracked_spend(accounting, monkeypatch):
    monkeypatch.setattr(accounting.db['aiUsageEvents'], 'insert_one', AsyncMock(side_effect=RuntimeError('DB transient')))
    provider = Mock(return_value=('ok', {'input_tokens': 10, 'output_tokens': 1}))
    with pytest.raises(RuntimeError, match='Unable to record'):
        await call(accounting, provider)
    provider.assert_not_called()


async def test_accounting_failure_does_not_break_ai_result(accounting, monkeypatch):
    monkeypatch.setattr(accounting.db['aiUsageEvents'], 'update_one', AsyncMock(side_effect=RuntimeError('DB down')))
    result, _ = await call(accounting, Mock(return_value=('ok', {'input_tokens': 10, 'output_tokens': 1})))
    assert result == 'ok'
    assert accounting.db['aiUsageEvents'].docs[0]['usage_status'] == 'unknown'


@pytest.mark.parametrize('stage', ['parse', 'pdf', 'upload', 'save'])
async def test_evaluation_workflow_keeps_ai_cost_after_failure(accounting, monkeypatch, stage):
    s = accounting
    monkeypatch.setattr(grading, 'get_database', lambda: s.db)
    monkeypatch.setattr(grading, 'update_job', AsyncMock())
    monkeypatch.setattr(grading, '_resolve_institute_id', AsyncMock(return_value=s.institute))
    monkeypatch.setattr(grading, '_download_pdf', Mock(return_value=b'pdf'))
    monkeypatch.setattr(grading, 'ensure_mcq_answer_key', AsyncMock(return_value={}))
    monkeypatch.setattr(grading, 'extract_answer_text_with_gemini', Mock(return_value=(
        'Answer', {'prompt_tokens': 10, 'candidate_tokens': 5, 'total_tokens': 15, 'model': 'gemini-2.5-flash'})))
    monkeypatch.setattr(grading, 'grade_with_claude', Mock(return_value=(
        'AI JSON', {'input_tokens': 20, 'output_tokens': 10, 'total_tokens': 30, 'model': 'claude-sonnet-4-6'})))
    monkeypatch.setattr(grading, 'safe_json_parse', Mock(return_value={'questionwise_marking': []}))
    monkeypatch.setattr(grading, 'generate_evaluation_report_html', Mock(return_value='html'))
    monkeypatch.setattr(grading, 'render_html_to_pdf', Mock(return_value=b'pdf'))
    monkeypatch.setattr(grading, 'upload_file_to_imagekit', Mock(return_value={'url': 'https://cdn.test/report.pdf', 'file_id': 'r'}))
    if stage == 'save':
        monkeypatch.setattr(s.db['answerDetails'], 'update_one', AsyncMock(side_effect=RuntimeError('DB save failed')))
    else:
        function = {'parse': 'safe_json_parse', 'pdf': 'render_html_to_pdf', 'upload': 'upload_file_to_imagekit'}[stage]
        monkeypatch.setattr(grading, function, Mock(side_effect=RuntimeError(stage + ' failed')))
    await grading._run_evaluation_job('failed-job', s.exam, s.answer, False, s.faculty, s.user)
    assert grading.update_job.await_args.args[2]['status'] == 'error'
    assert len(s.db['aiUsageEvents'].docs) == 2
    assert all(event['cost_usd'] > 0 for event in s.db['aiUsageEvents'].docs)
    assert get(s.db['instituteDetails'].docs[0], 'token_usage.grand_total_tokens') == 45


async def test_generation_workflow_keeps_cost_when_docx_build_fails(accounting, monkeypatch):
    s = accounting
    monkeypatch.setattr(qp, 'get_database', lambda: s.db)
    monkeypatch.setattr(qp, 'update_job', AsyncMock())
    monkeypatch.setattr(qp, '_build_claude_prompt', Mock(return_value='prompt'))
    monkeypatch.setattr(qp, 'generate_text', Mock(return_value=('Paper', {'input_tokens': 20, 'output_tokens': 10})))
    monkeypatch.setattr(qp, 'build_docx', Mock(side_effect=RuntimeError('DOCX failed')))
    params = dict.fromkeys(['prompt', 'institute_name', 'department_name', 'subject_name', 'exam_type',
                           'semester', 'academic_year', 'duration'], 'test')
    params.update(faculty_id=s.faculty, user_id=s.user, total_marks=10, sections=[])
    await qp._run_generation_job('failed-generation', params, {})
    assert qp.update_job.await_args.args[2]['status'] == 'failed'
    assert s.db['aiUsageEvents'].docs[0]['cost_usd'] > 0
    assert get(s.db['instituteDetails'].docs[0], 'token_usage.claude.total_tokens') == 30


async def test_extraction_cost_survives_text_save_failure(accounting, monkeypatch):
    from app.services import gemini
    s = accounting
    s.db['newsavedDocs'].docs[0]['question_paper']['url'] = 'https://cdn.test/paper.pdf'
    monkeypatch.setattr(gemini, 'safe_get', Mock(return_value=b'pdf'))
    monkeypatch.setattr(gemini, 'extract_question_paper_text_with_gemini', Mock(return_value=(
        'Text', {'prompt_tokens': 10, 'candidate_tokens': 5, 'total_tokens': 15})))
    original = s.db['newsavedDocs'].update_one
    async def fail_text_save(query, changes, **kwargs):
        if 'question_paper.text' in changes.get('$set', {}):
            raise RuntimeError('Text save failed')
        return await original(query, changes, **kwargs)
    monkeypatch.setattr(s.db['newsavedDocs'], 'update_one', fail_text_save)
    await gemini.extract_and_patch_question_paper_text(s.db, ObjectId(s.exam), 'https://cdn.test/paper.pdf',
                                                     s.faculty, user_id=s.user, paper_version='A')
    assert s.db['aiUsageEvents'].docs[0]['cost_usd'] > 0
    assert get(s.db['instituteDetails'].docs[0], 'token_usage.gemini.total_tokens') == 15


async def test_key_cost_survives_key_save_failure(accounting, monkeypatch):
    from app.services import mcq_grading as mcq
    s = accounting
    monkeypatch.setattr(mcq, 'detect_mcq_questions', Mock(return_value={1: ['a', 'b', 'c', 'd']}))
    monkeypatch.setattr(mcq, 'determine_mcq_answer_keys', Mock(return_value=(
        {'1': {'answer': 'A', 'confident': True}}, {'input_tokens': 10, 'output_tokens': 5})))
    monkeypatch.setattr(s.db['newsavedDocs'], 'update_one', AsyncMock(side_effect=RuntimeError('Key save failed')))
    key = await mcq.determine_and_save_mcq_answer_keys(s.db, ObjectId(s.exam), 'Question', s.faculty,
                                                     s.user, paper_version='A')
    assert key == {}
    assert s.db['aiUsageEvents'].docs[0]['cost_usd'] > 0
    assert get(s.db['instituteDetails'].docs[0], 'token_usage.claude.total_tokens') == 15


@pytest.mark.parametrize('kind', ['text', 'html'])
async def test_provider_usage_survives_response_content_error(accounting, monkeypatch, kind):
    from app.services import claude
    message = SimpleNamespace(content=[], usage=SimpleNamespace(input_tokens=20, output_tokens=10))
    client = Mock()
    client.messages.create.return_value = message
    client.messages.stream.return_value.__enter__ = Mock(return_value=SimpleNamespace(get_final_message=lambda: message))
    client.messages.stream.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(claude, '_get_client', lambda: client)
    function = lambda: (claude.generate_text('prompt') if kind == 'text' else claude.generate_html('prompt'))
    with pytest.raises(RuntimeError):
        await call(accounting, function)
    event = accounting.db['aiUsageEvents'].docs[0]
    assert event['usage_status'] == 'known' and event['call_status'] == 'failed'
    assert event['total_tokens'] == 30 and event['cost_usd'] > 0


async def test_grading_fallback_preserves_available_usage(accounting, monkeypatch):
    error = RuntimeError('Response formatting failed')
    error.usage = {'input_tokens': 20, 'output_tokens': 10, 'total_tokens': 30}
    monkeypatch.setattr(grading_service, 'generate_text', Mock(side_effect=error))
    await call(accounting, lambda: grading_service.grade_with_claude('q', 'a', [{'maxMarks': 10}]))
    event = accounting.db['aiUsageEvents'].docs[0]
    assert event['usage_status'] == 'known' and event['call_status'] == 'failed'
    assert event['cost_usd'] > 0


async def test_institute_rollup_attempted_even_if_user_rollup_fails(accounting, monkeypatch):
    monkeypatch.setattr(accounting.db['users'], 'update_one', AsyncMock(side_effect=RuntimeError('User rollup failed')))
    await call(accounting, Mock(return_value=('ok', {'input_tokens': 20, 'output_tokens': 10})))
    assert accounting.db['aiUsageEvents'].docs[0]['cost_usd'] > 0
    assert get(accounting.db['instituteDetails'].docs[0], 'token_usage.claude.total_tokens') == 30


async def test_legacy_usage_writer_still_skips_zero_and_does_not_double_institute(accounting):
    from app.services.ai_usage import record_ai_usage
    kwargs = dict(user_id=accounting.user, provider='claude', model='claude-sonnet-4-6', feature='legacy')
    await record_ai_usage(accounting.db, **kwargs, usage={'input_tokens': 0, 'output_tokens': 0})
    assert accounting.db['aiUsageEvents'].docs == []
    await record_ai_usage(accounting.db, **kwargs, usage={'input_tokens': 20, 'output_tokens': 10})
    assert len(accounting.db['aiUsageEvents'].docs) == 1
    assert 'token_usage' not in accounting.db['instituteDetails'].docs[0]
