"""Paper replacement regressions with fake DB, AI, queue and storage."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from bson import ObjectId
from fastapi import BackgroundTasks

from app.api.routers import exams, grading
from app.schemas.exams import UploadQuestionPaperRequest
from app.services import gemini, mcq_grading as mcq
from app.services.paper_version import paper_key_version


def value(doc, dotted):
    for key in dotted.split('.'):
        if not isinstance(doc, dict):
            return None
        doc = doc.get(key)
    return doc


class Papers:
    def __init__(self, doc):
        self.doc = doc

    def matches(self, query):
        return self.doc is not None and all(value(self.doc, key) == val for key, val in query.items())

    async def find_one(self, query):
        return deepcopy(self.doc) if self.matches(query) else None

    async def update_one(self, query, change):
        if not self.matches(query):
            return SimpleNamespace(matched_count=0)
        for key, val in change.get('$set', {}).items():
            parts = key.split('.')
            target = self.doc
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = deepcopy(val)
        for key in change.get('$unset', {}):
            self.doc.pop(key, None)
        return SimpleNamespace(matched_count=1)


@pytest.fixture
def paper(monkeypatch):
    exam_id, faculty, user = [ObjectId() for _ in range(3)]
    collection = Papers({'_id': exam_id, 'faculty_id': faculty, 'question_paper': {
        'version': 'A', 'url': 'https://cdn.test/A.pdf', 'fileId': 'A', 'text': 'Paper A'},
        'mcq_answer_key': {'1': {'answer': 'A', 'confident': True}}, 'mcq_answer_key_version': 'A'})
    db = {'newsavedDocs': collection}
    monkeypatch.setattr(exams, '_require_faculty', AsyncMock(return_value=({'_id': user}, faculty)))
    monkeypatch.setattr(exams, 'enqueue', AsyncMock())
    monkeypatch.setattr(gemini, 'safe_get', Mock(return_value=b'paper'))
    monkeypatch.setattr(gemini, 'extract_question_paper_text_with_gemini', Mock(return_value=(
        'Extracted A', {'prompt_tokens': 1, 'candidate_tokens': 1})))
    monkeypatch.setattr(gemini, 'increment_institute_gemini_tokens', AsyncMock())
    monkeypatch.setattr(mcq, 'detect_mcq_questions', Mock(return_value={1: ['a', 'b', 'c', 'd']}))
    monkeypatch.setattr(mcq, 'determine_mcq_answer_keys', Mock(return_value=(
        {'1': {'answer': 'B', 'confident': True}}, {})))
    return SimpleNamespace(db=db, collection=collection, exam=exam_id, faculty=str(faculty), user=str(user))


def replace_paper(p):
    p.collection.doc['question_paper'] = {'version': 'B', 'url': 'https://cdn.test/B.pdf', 'text': 'Paper B'}
    p.collection.doc['mcq_answer_key'] = {'1': {'answer': 'D', 'confident': True}}
    p.collection.doc['mcq_answer_key_version'] = 'B'


@pytest.mark.parametrize('allowed', [False, True])
async def test_upload_invalidates_old_key_and_passes_unique_version(paper, monkeypatch, allowed):
    monkeypatch.setattr(exams, 'check_institute_token_budget', AsyncMock(return_value={
        'allowed': allowed, 'warnings': [], 'message': 'Budget exhausted'}))
    payload = UploadQuestionPaperRequest(questionpaper_url='https://cdn.test/B.pdf', fileId='B', filename='B.pdf', no_of_question=1)
    versions = []
    for _ in range(2):
        await exams.upload_question_paper(str(paper.exam), BackgroundTasks(), payload, {}, paper.db)
        doc = paper.collection.doc
        versions.append(doc['question_paper']['version'])
        assert 'mcq_answer_key' not in doc and 'mcq_answer_key_version' not in doc
        assert doc['question_paper']['text'] is None
        if allowed:
            assert exams.enqueue.await_args.args[-1] == versions[-1]
    assert versions[0] != versions[1]
    if not allowed:
        exams.enqueue.assert_not_awaited()


@pytest.mark.parametrize('outcome', ['success', 'error'])
async def test_late_extraction_cannot_change_replacement(paper, monkeypatch, outcome):
    before = None
    def extract(*args):
        nonlocal before
        replace_paper(paper)
        before = deepcopy(paper.collection.doc)
        if outcome == 'error':
            raise RuntimeError('old extraction failed')
        return 'OLD TEXT', {'prompt_tokens': 1, 'candidate_tokens': 1}
    monkeypatch.setattr(gemini, 'extract_question_paper_text_with_gemini', extract)
    resolve = AsyncMock()
    monkeypatch.setattr(mcq, 'determine_and_save_mcq_answer_keys', resolve)
    await gemini.extract_and_patch_question_paper_text(paper.db, paper.exam, 'https://cdn.test/A.pdf', paper.faculty, paper_version='A')
    assert paper.collection.doc == before
    resolve.assert_not_awaited()


async def test_current_extraction_saves_text_and_clears_error(paper, monkeypatch):
    paper.collection.doc['question_paper']['text_error'] = 'previous error'
    resolve = AsyncMock(return_value={})
    monkeypatch.setattr(mcq, 'determine_and_save_mcq_answer_keys', resolve)
    await gemini.extract_and_patch_question_paper_text(paper.db, paper.exam, 'https://cdn.test/A.pdf', paper.faculty, paper_version='A')
    assert paper.collection.doc['question_paper']['text'] == 'Extracted A'
    assert paper.collection.doc['question_paper']['text_error'] is None
    assert resolve.await_args.kwargs['paper_version'] == 'A'


@pytest.mark.parametrize('legacy', [False, True])
async def test_stale_job_is_skipped_before_download(paper, monkeypatch, legacy):
    replace_paper(paper)
    before = deepcopy(paper.collection.doc)
    await gemini.extract_and_patch_question_paper_text(paper.db, paper.exam, 'https://cdn.test/A.pdf', paper.faculty,
                                                     paper_version=None if legacy else 'A')
    gemini.safe_get.assert_not_called()
    assert paper.collection.doc == before


async def test_old_queue_job_can_extract_matching_unversioned_paper(paper, monkeypatch):
    paper.collection.doc['question_paper'].pop('version')
    monkeypatch.setattr(mcq, 'determine_and_save_mcq_answer_keys', AsyncMock())
    await gemini.extract_and_patch_question_paper_text(paper.db, paper.exam, 'https://cdn.test/A.pdf', paper.faculty)
    assert paper.collection.doc['question_paper']['text'] == 'Extracted A'


@pytest.mark.parametrize('legacy', [False, True])
async def test_current_key_is_cached_and_tagged(paper, legacy):
    if legacy:
        paper.collection.doc['question_paper'].pop('version')
        paper.collection.doc.pop('mcq_answer_key_version')
    snapshot = deepcopy(paper.collection.doc)
    result = await mcq.ensure_mcq_answer_key(paper.db, snapshot, paper.faculty)
    assert result
    assert paper.collection.doc['mcq_answer_key_version'] == paper_key_version(snapshot['question_paper'])
    # Legacy untagged keys are rebuilt once; a correctly tagged key is reused.
    assert mcq.determine_mcq_answer_keys.call_count == int(legacy)
    await mcq.ensure_mcq_answer_key(paper.db, deepcopy(paper.collection.doc), paper.faculty)
    assert mcq.determine_mcq_answer_keys.call_count == int(legacy)


@pytest.mark.parametrize('failure', [False, True])
async def test_mismatched_key_is_never_returned(paper, failure):
    paper.collection.doc['mcq_answer_key_version'] = 'OLD'
    if failure:
        mcq.determine_mcq_answer_keys.side_effect = RuntimeError('provider failed')
    result = await mcq.ensure_mcq_answer_key(paper.db, deepcopy(paper.collection.doc), paper.faculty)
    assert result == {} if failure else result['1']['answer'] == 'B'


@pytest.mark.parametrize('legacy', [False, True])
async def test_late_key_generation_cannot_save_or_return_old_key(paper, monkeypatch, legacy):
    if legacy:
        paper.collection.doc['question_paper'].pop('version')
        paper.collection.doc.pop('mcq_answer_key_version')
    else:
        paper.collection.doc['mcq_answer_key_version'] = 'OLD'
    snapshot = deepcopy(paper.collection.doc)
    def resolve(*args):
        replace_paper(paper)
        return {'1': {'answer': 'B', 'confident': True}}, {}
    monkeypatch.setattr(mcq, 'determine_mcq_answer_keys', resolve)
    result = await mcq.ensure_mcq_answer_key(paper.db, snapshot, paper.faculty)
    assert result == {}
    assert paper.collection.doc['mcq_answer_key']['1']['answer'] == 'D'
    assert paper.collection.doc['mcq_answer_key_version'] == 'B'


async def test_stale_exam_snapshot_does_not_return_cached_key(paper):
    snapshot = deepcopy(paper.collection.doc)
    replace_paper(paper)
    assert await mcq.ensure_mcq_answer_key(paper.db, snapshot, paper.faculty) == {}


async def test_subjective_only_paper_does_not_call_key_provider(paper):
    mcq.detect_mcq_questions.return_value = {}
    paper.collection.doc.pop('mcq_answer_key_version')
    assert await mcq.ensure_mcq_answer_key(paper.db, deepcopy(paper.collection.doc), paper.faculty) == {}
    mcq.determine_mcq_answer_keys.assert_not_called()


async def test_current_extraction_error_is_recorded(paper, monkeypatch):
    gemini.safe_get.side_effect = RuntimeError('Download failed')
    await gemini.extract_and_patch_question_paper_text(paper.db, paper.exam, 'https://cdn.test/A.pdf', paper.faculty, paper_version='A')
    assert paper.collection.doc['question_paper']['text_error'] == 'Download failed'


async def test_same_url_reupload_is_still_a_new_version(paper):
    snapshot = deepcopy(paper.collection.doc)
    paper.collection.doc['question_paper']['version'] = 'B'
    assert await mcq.ensure_mcq_answer_key(paper.db, snapshot, paper.faculty) == {}
    await gemini.extract_and_patch_question_paper_text(paper.db, paper.exam, 'https://cdn.test/A.pdf', paper.faculty, paper_version='A')
    gemini.safe_get.assert_not_called()


async def test_deleted_exam_does_not_receive_late_text_or_key(paper, monkeypatch):
    snapshot = deepcopy(paper.collection.doc)
    def extract(*args):
        paper.collection.doc = None
        return 'OLD TEXT', {'prompt_tokens': 0, 'candidate_tokens': 0}
    monkeypatch.setattr(gemini, 'extract_question_paper_text_with_gemini', extract)
    await gemini.extract_and_patch_question_paper_text(paper.db, paper.exam, 'https://cdn.test/A.pdf', paper.faculty, paper_version='A')
    assert paper.collection.doc is None
    assert await mcq.ensure_mcq_answer_key(paper.db, snapshot, paper.faculty) == {}


async def test_grading_stops_if_paper_changes_before_ocr(paper, monkeypatch):
    answer_id = ObjectId()
    db = {**paper.db,
          'answerDetails': SimpleNamespace(find_one=AsyncMock(return_value={
              '_id': answer_id, 'exam_id': paper.exam, 'answer_script_url': 'https://cdn.test/student.pdf'})),
          'evaluationDetails': SimpleNamespace(find_one=AsyncMock(return_value={
              'questionEvaluationDetails': [{'maxMarks': 10}]}))}
    monkeypatch.setattr(grading, 'get_database', lambda: db)
    updates = AsyncMock()
    monkeypatch.setattr(grading, 'update_job', updates)
    monkeypatch.setattr(grading, '_resolve_institute_id', AsyncMock(return_value=ObjectId()))
    monkeypatch.setattr(grading, '_download_pdf', Mock(return_value=b'pdf'))
    async def change_paper(*args):
        replace_paper(paper)
        return {}
    monkeypatch.setattr(grading, 'ensure_mcq_answer_key', change_paper)
    ocr = Mock()
    monkeypatch.setattr(grading, 'extract_answer_text_with_gemini', ocr)
    await grading._run_evaluation_job('test', str(paper.exam), str(answer_id), False, paper.faculty)
    ocr.assert_not_called()
    final = updates.await_args.args[2]
    assert final['status'] == 'error'
    assert 'Question paper changed' in final['message']
