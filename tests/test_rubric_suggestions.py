"""Offline rubric preparation contracts. No database server or provider calls."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from bson import ObjectId
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_identity
from app.api.routers import evaluation
from app.core import queue
from app.core.config import settings
from app.db.mongodb import get_database
from app.models.ai_usage_event import FACULTY_FEATURES, FEATURE_LABELS, Feature
from app.services import rubric_suggestions as service
from app.utils.ai_pricing import estimate_cost_usd


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    def forbidden(*args, **kwargs):
        raise AssertionError("Network calls are forbidden in rubric suggestion tests")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


PAPER = "Exam\nQ1. Define supervised learning. [2 Marks] [CO1]\nQ2. Explain normalization. [3 Marks] [CO1, CO2]"


def context():
    return {"questions": service.question_blocks(PAPER, 2), "paper_text": PAPER,
            "available_cos": [{"co_code": "CO1", "description": "Concepts"}, {"co_code": "CO2", "description": "Application"}]}


def answer():
    return {"questions": [{"question_no": n, "status": "draft_ready", "max_marks": marks,
        "max_quote": f"[{marks} Marks]", "min_marks": 0, "min_source": "default", "min_quote": "",
        "co_tags": [{"code": "CO1", "quote": "[CO1]" if n == 1 else "[CO1, CO2]"}],
        "parameters": [{"name": "Correct content", "percentage": 100, "isCustom": True}],
        "guidelines": "Award credit for correct concepts; partial for valid incomplete concepts, zero otherwise.",
        "notes": []} for n, marks in [(1, 2), (2, 3)]]}


def test_printed_marks_and_known_cos_are_reviewable():
    result = service.validate_suggestions(json.dumps(answer()), context())
    assert result["model"] == "claude-sonnet-5-5"
    assert result["questions"][0]["co_tags"][0]["eligible"] is True
    assert result["questions"][0]["min_source"] == "default"


@pytest.mark.parametrize("change", [
    lambda d: d["questions"].reverse(),
    lambda d: d["questions"].pop(),
    lambda d: d["questions"][0].update(max_marks=5),
    lambda d: d["questions"][0].update(max_quote="[3 Marks]"),  # Exists only under Q2.
    lambda d: d["questions"][0].update(min_marks=1),
    lambda d: d["questions"][0].update(min_marks=True),
    lambda d: d["questions"][0]["parameters"][0].update(percentage=True),
    lambda d: d["questions"][0]["parameters"][0].update(percentage=90),
    lambda d: d["questions"][0]["parameters"].append({"name": "correct CONTENT", "percentage": 1, "isCustom": True}),
    lambda d: d["questions"][0].update(guidelines=""),
    lambda d: d["questions"][0]["co_tags"][0].update(code="CO2", quote="[CO1, CO2]"),
    lambda d: d["questions"][0].update(status="needs_clarification"),
])
def test_invalid_or_misattributed_output_is_rejected(change):
    data = answer()
    change(data)
    if [q["question_no"] for q in data["questions"]] != [1, 2]:
        with pytest.raises(ValueError):
            service.validate_suggestions(json.dumps(data), context())
    else:
        rows = service.validate_suggestions(json.dumps(data), context())["questions"]
        assert rows[0]["status"] == "needs_clarification"
        assert rows[0]["parameters"] == [] and rows[0]["max_marks"] is None
        assert rows[1]["status"] == "draft_ready"


def test_missing_marks_produce_clarification_not_inferred_scores():
    data = answer()
    data["questions"][0].update(status="needs_clarification", max_marks=None, max_quote="",
        parameters=[], guidelines="", notes=["Supply the question maximum."])
    row = service.validate_suggestions(json.dumps(data), context())["questions"][0]
    assert row["max_marks"] is None and row["parameters"] == []


def test_unknown_co_is_retained_as_evidence_but_not_eligible():
    ctx = context()
    ctx["available_cos"] = []
    row = service.validate_suggestions(json.dumps(answer()), ctx)["questions"][0]
    assert row["co_tags"][0]["eligible"] is False
    assert row["notes"]


def test_minimum_score_is_not_a_passing_threshold():
    ctx = context()
    ctx["questions"][0]["text"] += " Minimum pass marks: 1"
    data = answer()
    data["questions"][0].update(min_source="explicit", min_marks=1, min_quote="Minimum pass marks: 1")
    row = service.validate_suggestions(json.dumps(data), ctx)["questions"][0]
    assert row["status"] == "needs_clarification" and row["min_marks"] == 0


@pytest.mark.parametrize("text,count", [("Q1. x\nQ1. y", 2), (PAPER, 3)])
def test_ambiguous_numbering_is_not_silently_mapped(text, count):
    with pytest.raises(Exception) as error:
        service.question_blocks(text, count)
    assert error.value.status_code == 422


def test_plain_numbered_questions_are_supported():
    assert len(service.question_blocks("1. One\n2. Two", 2)) == 2


@pytest.mark.parametrize("heading", [
    "Question {n} (10 marks)",
    "Question {n}: Explain the concept.",
    "Question {n}",
    "Q{n}",
    "Q{n} Explain the concept.",
    "QUESTION {n}: Explain the concept.",
    "Q{n}) Explain the concept.",
])
def test_extracted_heading_formats_preserve_question_boundaries(heading):
    text = "Instructions:\n1. Answer both questions.\n2. Show relevant work.\n"
    text += heading.format(n=1) + "\nFirst question content.\n(a) First part.\n"
    text += heading.format(n=2) + "\nSecond question content."
    blocks = service.question_blocks(text, 2)
    assert [b["question_no"] for b in blocks] == [1, 2]
    assert blocks[0]["text"].startswith(heading.format(n=1))
    assert "(a) First part." in blocks[0]["text"]
    assert "Answer both questions" not in blocks[0]["text"]
    assert "Second question content" not in blocks[0]["text"]
    assert blocks[1]["text"].endswith("Second question content.")


def test_standalone_headings_support_crlf_and_multi_digit_numbers():
    text = "\r\n".join(f"Question {n}\r\nContent {n}" for n in range(1, 13))
    assert [b["question_no"] for b in service.question_blocks(text, 12)] == list(range(1, 13))


def test_attached_subpart_labels_remain_inside_top_level_questions():
    text = "Question 1 (10 marks)\nQ1(a) Explain A.\nQ1(b) Explain B.\nQ1.1 Explain a nested part.\nQuestion 2: Explain C."
    blocks = service.question_blocks(text, 2)
    assert "Q1(a) Explain A." in blocks[0]["text"]
    assert "Q1(b) Explain B." in blocks[0]["text"]
    assert "Q1.1 Explain a nested part." in blocks[0]["text"]


@pytest.mark.parametrize("text,count", [
    ("Question 1 (10 marks)\nFirst\nQuestion 1 (10 marks)\nAgain", 2),
    ("Question 1\nFirst\nQuestion 2\nSecond", 3),
    ("1. First\n2. Second\nQuestion 1 (10 marks)\nIncomplete explicit set", 2),
])
def test_flexible_headings_still_reject_ambiguous_question_mappings(text, count):
    with pytest.raises(Exception) as error:
        service.question_blocks(text, count)
    assert error.value.status_code == 422


@pytest.mark.parametrize("label", ["Q", "Question "])
def test_explicit_question_labels_ignore_numbered_instructions(label):
    text = "INSTRUCTIONS:\n1. Attempt all questions.\n2. Write clearly.\n\n"
    text += f"{label}1. Define learning. [2 Marks] [CO1]\n"
    text += f"{label}2. Explain scaling. [3 Marks] [CO2]"
    blocks = service.question_blocks(text, 2)
    assert [b["question_no"] for b in blocks] == [1, 2]
    assert blocks[0]["text"].startswith(f"{label}1.")
    assert "Attempt all questions" not in blocks[0]["text"]
    assert "[2 Marks] [CO1]" in blocks[0]["text"]
    assert "[3 Marks] [CO2]" not in blocks[0]["text"]


def test_numbered_content_inside_explicit_question_stays_in_its_block():
    text = "Q1. Explain these steps.\n1. Clean data.\n2. Scale data.\nQ2. Define a label."
    blocks = service.question_blocks(text, 2)
    assert "1. Clean data." in blocks[0]["text"]
    assert "2. Scale data." in blocks[0]["text"]
    assert "Clean data" not in blocks[1]["text"]


@pytest.mark.parametrize("text", [
    "1. First\n2. Second\nQ1. Incomplete explicit set.",
    "1. First\n2. Second\nQ1. Duplicated\nQ1. Again",
])
def test_invalid_explicit_labels_cannot_fall_back_to_plain_numbering(text):
    with pytest.raises(Exception) as error:
        service.question_blocks(text, 2)
    assert error.value.status_code == 422


def test_suggestion_job_is_registered_for_worker_dispatch():
    from app.worker import _FUNCTIONS
    assert sum(fn.__name__ == "run_rubric_suggestion" for fn in _FUNCTIONS) == 1


def test_provider_call_is_scoped_bounded_and_has_no_temperature(monkeypatch):
    provider = Mock()
    provider.messages.create.return_value = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(answer()))],
        usage=SimpleNamespace(input_tokens=10, output_tokens=20))
    monkeypatch.setattr(service, "_get_client", lambda: provider)
    raw, usage = service.generate_suggestions(context())
    sent = provider.messages.create.call_args.kwargs
    assert sent["model"] == "claude-sonnet-5-5"
    assert "temperature" not in sent and sent["max_tokens"] <= 24000
    assert sent["extra_body"]["output_config"]["format"]["type"] == "json_schema"
    assert usage["total_tokens"] == 30 and json.loads(raw) == answer()


@pytest.mark.parametrize("stop", ["max_tokens", "refusal"])
def test_incomplete_provider_response_preserves_usage(monkeypatch, stop):
    provider = Mock()
    provider.messages.create.return_value = SimpleNamespace(stop_reason=stop, usage=SimpleNamespace(input_tokens=10, output_tokens=20))
    monkeypatch.setattr(service, "_get_client", lambda: provider)
    with pytest.raises(RuntimeError) as error:
        service.generate_suggestions(context())
    assert error.value.usage["output_tokens"] == 20
    assert provider.messages.create.call_count == 1


def test_new_feature_has_price_and_faculty_accounting():
    assert Feature.RUBRIC_SUGGESTION in FACULTY_FEATURES
    assert Feature.RUBRIC_SUGGESTION in FEATURE_LABELS
    assert estimate_cost_usd(service.MODEL, 1000, 1000) == pytest.approx(0.012)
    from app.services.grading import CLAUDE_GRADING_MODEL, CLAUDE_TRANSCRIPT_MODEL
    assert CLAUDE_GRADING_MODEL == "claude-sonnet-5-5"
    assert CLAUDE_TRANSCRIPT_MODEL == "claude-sonnet-4-5"


class Collection:
    def __init__(self, docs=None):
        self.docs = deepcopy(docs or [])

    @staticmethod
    def matches(doc, query):
        for key, expected in query.items():
            value = doc
            for part in key.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            if isinstance(expected, dict) and "$or" not in expected:
                if "$ne" in expected and value == expected["$ne"]: return False
                if "$gt" in expected and (value is None or value <= expected["$gt"]): return False
                if "$exists" in expected and (value is not None) != expected["$exists"]: return False
            elif value != expected: return False
        return True

    async def find_one(self, query, *args, **kwargs):
        # Fixtures have no prior marks, so the legacy-protection query is empty.
        if "$or" in query: return None
        return next((deepcopy(d) for d in self.docs if self.matches(d, query)), None)

    async def update_one(self, query, update, upsert=False):
        doc = next((d for d in self.docs if self.matches(d, query)), None)
        inserted = None
        if doc is None and upsert:
            doc = {**query, **deepcopy(update.get("$setOnInsert", {}))}
            self.docs.append(doc)
            inserted = doc["_id"]
        if doc is not None:
            for key, value in update.get("$set", {}).items():
                target = doc
                parts = key.split(".")
                for part in parts[:-1]:
                    target = target.setdefault(part, {})
                target[parts[-1]] = deepcopy(value)
            for key in update.get("$unset", {}): doc.pop(key, None)
        return SimpleNamespace(matched_count=int(doc is not None), modified_count=int(doc is not None), upserted_id=inserted)


@pytest.fixture
async def isolated(monkeypatch):
    user, faculty, exam_id, subject = (ObjectId() for _ in range(4))
    exam = {"_id": exam_id, "faculty_id": faculty, "subject_id": subject, "covered_cos": ["CO1"],
            "question_paper": {"text": PAPER, "version": "v1", "no_of_questions": 2}}
    db = {"newsavedDocs": Collection([exam]), "facultyDetails": Collection([{"_id": faculty, "user_id": user}]),
          "users": Collection([{"_id": user, "role": 3, "is_active": True}]),
          "answerDetails": Collection(), "subjectDetails": Collection([{"_id": subject, "co": [{"co_code": "CO1", "description": "Concepts"}]}]),
          service.COLLECTION: Collection()}
    app = FastAPI()
    app.include_router(evaluation.router)
    async def identity(request: Request):
        return {"user_id": str(user if request.headers.get("actor", "owner") == "owner" else ObjectId()),
                "role": 2 if request.headers.get("actor") == "admin" else 3}
    app.dependency_overrides[get_current_identity] = identity
    app.dependency_overrides[get_database] = lambda: db
    pending = []
    async def enqueue(*args, **kwargs): pending.append(args)
    monkeypatch.setattr(queue, "enqueue", enqueue)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-for-offline-test")
    from app.utils import token_usage
    async def budget(*args): return {"allowed": True, "message": None, "warnings": []}
    monkeypatch.setattr(token_usage, "check_institute_token_budget", budget)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(db=db, client=client, pending=pending, exam=exam,
                              url=f"/evaluation-details/{exam_id}/suggestions")


async def test_generation_is_explicit_and_concurrent_requests_reuse_one_job(isolated):
    s = isolated
    assert (await s.client.get(s.url)).json()["status"] == "not_started"
    assert s.pending == []
    responses = await asyncio.gather(*(s.client.post(s.url, json={}) for _ in range(5)))
    assert all(r.status_code == 200 for r in responses)
    assert len(s.pending) == 1
    assert len({r.json()["source_id"] for r in responses}) == 1


async def test_prompt_upgrade_does_not_reuse_or_automatically_regenerate_old_draft(isolated, monkeypatch):
    s = isolated
    current_version = service.PROMPT_VERSION
    monkeypatch.setattr(service, "PROMPT_VERSION", "rubric-preparation-v1")
    old_response = (await s.client.post(s.url, json={})).json()
    old_row = s.db[service.COLLECTION].docs[0]
    old_row.update(status="completed", result=service.validate_suggestions(json.dumps(answer()), context()))
    old_snapshot = deepcopy(old_row)

    monkeypatch.setattr(service, "PROMPT_VERSION", current_version)
    assert (await s.client.get(s.url)).json()["status"] == "not_started"
    assert len(s.pending) == 1  # Reading after upgrade never queues a paid call.
    assert old_row == old_snapshot

    new_response = (await s.client.post(s.url, json={})).json()
    assert new_response["source_id"] != old_response["source_id"]
    assert len(s.pending) == 2
    assert s.db[service.COLLECTION].docs[0] == old_snapshot
    assert s.pending[-1][-1]["prompt_version"] == current_version


@pytest.mark.parametrize("actor", ["other", "admin"])
async def test_other_users_cannot_request_or_read_owner_drafts(isolated, actor):
    for method in (isolated.client.get, isolated.client.post):
        response = await method(isolated.url, headers={"actor": actor},
                                **({"json": {}} if method == isolated.client.post else {}))
        assert response.status_code in (400, 401, 403, 404)
    assert isolated.pending == []


async def test_locked_exam_cannot_generate(isolated):
    isolated.db["newsavedDocs"].docs[0]["paper_locked"] = True
    assert (await isolated.client.post(isolated.url, json={})).status_code == 409
    assert isolated.pending == []


async def test_worker_records_draft_without_saving_rubric_and_reuses_it(isolated, monkeypatch):
    s = isolated
    monkeypatch.setattr("app.db.mongodb.get_database", lambda: s.db)
    calls = []
    async def tracked(*args, **kwargs):
        calls.append(kwargs)
        return json.dumps(answer()), {}
    monkeypatch.setattr(service, "tracked_faculty_call", tracked)
    await s.client.post(s.url, json={})
    _, job_id, attempt_id, ctx = s.pending[0]
    await service.run_suggestion_job(job_id, attempt_id, ctx)
    await service.run_suggestion_job(job_id, attempt_id, ctx)
    row = (await s.client.get(s.url)).json()
    assert row["status"] == "completed" and len(row["result"]["questions"]) == 2
    assert len(calls) == 1 and calls[0]["feature"] == Feature.RUBRIC_SUGGESTION
    assert "evaluationDetails" not in s.db
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    assert (await s.client.post(s.url, json={})).json()["reused"] is True
    assert len(s.pending) == 1


async def test_failed_attempt_needs_explicit_retry(isolated):
    s = isolated
    await s.client.post(s.url, json={})
    row = s.db[service.COLLECTION].docs[0]
    row.update(status="failed", error="Failed")
    original_attempt = row["attempt_id"]
    assert (await s.client.post(s.url, json={})).json()["status"] == "failed"
    assert len(s.pending) == 1
    response = await s.client.post(s.url, json={"retry_failed": True})
    assert response.json()["attempt_id"] != original_attempt and len(s.pending) == 2


async def test_old_attempt_and_changed_paper_never_spend(isolated, monkeypatch):
    s = isolated
    monkeypatch.setattr("app.db.mongodb.get_database", lambda: s.db)
    tracked = Mock(side_effect=AssertionError("Paid call must not run"))
    monkeypatch.setattr(service, "tracked_faculty_call", tracked)
    await s.client.post(s.url, json={})
    _, job_id, attempt_id, ctx = s.pending[0]
    await service.run_suggestion_job(job_id, "wrong-attempt", ctx)
    s.db["newsavedDocs"].docs[0]["question_paper"]["version"] = "v2"
    await service.run_suggestion_job(job_id, attempt_id, ctx)
    tracked.assert_not_called()
    assert (await s.client.get(s.url)).json()["status"] == "not_started"


@pytest.mark.parametrize("changed", [False, True])
async def test_saving_applied_draft_checks_source_and_still_requires_save(isolated, monkeypatch, changed):
    s = isolated
    source_id = (await s.client.get(s.url)).json()["source_id"]
    committed = []
    async def commit(*args, **kwargs): committed.append(kwargs)
    monkeypatch.setattr(evaluation, "commit_rubric", commit)
    if changed:
        s.db["newsavedDocs"].docs[0]["question_paper"]["version"] = "changed"
    response = await s.client.post(s.url.removesuffix("/suggestions"), json={
        "suggestion_source_id": source_id, "totalMarks": 5,
        "questionEvaluationDetails": [{"minMarks": 0, "maxMarks": n, "parameters": [], "cos": [],
            "guidelines": "Faculty-reviewed criteria"} for n in (2, 3)]})
    assert response.status_code == (409 if changed else 200)
    assert committed == ([] if changed else [{"suggestion_source_id": source_id}])


async def test_budget_exhaustion_prevents_submission(isolated, monkeypatch):
    from app.utils import token_usage
    async def denied(*args): return {"allowed": False, "message": "Institute quota exhausted", "warnings": []}
    monkeypatch.setattr(token_usage, "check_institute_token_budget", denied)
    response = await isolated.client.post(isolated.url, json={})
    assert response.status_code == 402
    assert isolated.pending == [] and isolated.db[service.COLLECTION].docs == []


async def test_ambiguous_paper_can_be_confirmed_without_credits_and_reused(isolated):
    s = isolated
    paper = s.db["newsavedDocs"].docs[0]["question_paper"]
    paper["text"] = "Define learning. [2 Marks] [CO1]\nExplain scaling. [3 Marks] [CO1]"
    response = await s.client.post(s.url, json={})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "QUESTION_MAPPING_REQUIRED" and s.pending == []
    selected = [{"start": line["start"], "question_label": label}
                for line, label in zip(detail["lines"], ["Learning", "Scaling"])]
    mapping_url = s.url.replace("/suggestions", "/question-mapping")
    response = await s.client.post(mapping_url, json={"source_id": detail["mapping_source_id"], "questions": selected})
    assert response.status_code == 200 and s.pending == []
    assert (await s.client.get(s.url)).json()["status"] == "not_started"
    assert (await s.client.post(s.url, json={})).status_code == 200
    assert len(s.pending) == 1
    blocks = s.pending[0][-1]["questions"]
    assert [b["question_label"] for b in blocks] == ["Learning", "Scaling"]
    assert "scaling" not in blocks[0]["text"]


@pytest.mark.parametrize("case", ["changed", "locked", "other", "invalid"])
async def test_manual_mapping_rejects_stale_locked_unauthorized_and_invalid_requests(isolated, case):
    from app.services.question_mapping import source_key
    s = isolated
    paper = s.db["newsavedDocs"].docs[0]["question_paper"]
    payload = {"source_id": source_key(paper), "questions": [
        {"start": PAPER.index("Q1."), "question_label": "Q1"},
        {"start": PAPER.index("Q2."), "question_label": "Q2"}]}
    if case == "changed": paper["text"] += " Changed"
    if case == "locked": s.db["newsavedDocs"].docs[0]["paper_locked"] = True
    if case == "invalid": payload["questions"][1]["start"] = 1
    response = await s.client.post(s.url.replace("/suggestions", "/question-mapping"), json=payload,
                                   headers={"actor": "other" if case == "other" else "owner"})
    assert response.status_code in (401, 403, 404, 409, 422)
    assert "question_mapping" not in paper and s.pending == []


async def test_suggestion_save_attaches_verified_source_labels_and_text(isolated, monkeypatch):
    s = isolated
    source_id = (await s.client.get(s.url)).json()["source_id"]
    saved = []
    async def capture(db, exam, details, total_marks, **kwargs): saved.extend(details)
    monkeypatch.setattr(evaluation, "commit_rubric", capture)
    payload = {"suggestion_source_id": source_id, "totalMarks": 5,
        "questionEvaluationDetails": [{"minMarks": 0, "maxMarks": n, "parameters": [], "cos": []} for n in (2, 3)]}
    response = await s.client.post(s.url.replace("/suggestions", ""), json=payload)
    assert response.status_code == 200
    assert [q["question_label"] for q in saved] == ["Q1", "Q2"]
    assert "normalization" not in saved[0]["question_text"]
    payload["questionEvaluationDetails"][0]["question_label"] = "Q2"
    assert (await s.client.post(s.url.replace("/suggestions", ""), json=payload)).status_code == 409


async def test_mapping_from_old_paper_is_not_reused_after_text_change(isolated):
    from app.services.question_mapping import source_key
    s = isolated
    paper = s.db["newsavedDocs"].docs[0]["question_paper"]
    paper["question_mapping"] = {"source_id": source_key(paper), "questions": [
        {"start": PAPER.index("Q1."), "question_label": "Q1"},
        {"start": PAPER.index("Q2."), "question_label": "Q2"}]}
    paper["text"] = "Changed and unnumbered paper"
    response = await s.client.get(s.url)
    assert response.status_code == 422 and response.json()["detail"]["code"] == "QUESTION_MAPPING_REQUIRED"


async def test_raw_response_is_saved_before_validation_and_not_exposed(isolated, monkeypatch):
    s = isolated
    monkeypatch.setattr("app.db.mongodb.get_database", lambda: s.db)
    raw = json.dumps(answer())
    async def tracked(*args, **kwargs): return raw, {}
    monkeypatch.setattr(service, "tracked_faculty_call", tracked)
    original = service.validate_suggestions
    def verify_saved(response, ctx):
        assert s.db[service.COLLECTION].docs[0]["raw_response"] == raw
        return original(response, ctx)
    monkeypatch.setattr(service, "validate_suggestions", verify_saved)
    await s.client.post(s.url, json={})
    _, job_id, attempt, ctx = s.pending[0]
    await service.run_suggestion_job(job_id, attempt, ctx)
    response = (await s.client.get(s.url)).json()
    assert response["status"] == "completed"
    assert "raw_response" not in response and "response_saved_at" not in response


async def test_saved_failed_response_retry_never_calls_or_bills_ai(isolated, monkeypatch):
    from app.utils import token_usage
    s = isolated
    monkeypatch.setattr("app.db.mongodb.get_database", lambda: s.db)
    await s.client.post(s.url, json={})
    row = s.db[service.COLLECTION].docs[0]
    row.update(status="failed", raw_response=json.dumps(answer()))
    async def forbidden(*args, **kwargs): raise AssertionError("Saved response must not call AI or check a paid budget")
    monkeypatch.setattr(service, "tracked_faculty_call", forbidden)
    monkeypatch.setattr(token_usage, "check_institute_token_budget", forbidden)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    response = await s.client.post(s.url, json={"retry_failed": True})
    assert response.status_code == 200 and len(s.pending) == 2
    _, job_id, attempt, ctx = s.pending[-1]
    await service.run_suggestion_job(job_id, attempt, ctx)
    assert row["status"] == "completed"


async def test_schema_failure_keeps_raw_response_for_later_revalidation(isolated, monkeypatch):
    s = isolated
    monkeypatch.setattr("app.db.mongodb.get_database", lambda: s.db)
    async def tracked(*args, **kwargs): return "not-json", {}
    monkeypatch.setattr(service, "tracked_faculty_call", tracked)
    await s.client.post(s.url, json={})
    _, job_id, attempt, ctx = s.pending[0]
    await service.run_suggestion_job(job_id, attempt, ctx)
    row = s.db[service.COLLECTION].docs[0]
    assert row["status"] == "failed" and row["raw_response"] == "not-json"
    assert row["failure_type"] == "JSONDecodeError"


async def test_explicit_paid_regeneration_checks_budget_before_discarding_saved_response(isolated, monkeypatch):
    from app.utils import token_usage
    s = isolated
    await s.client.post(s.url, json={})
    row = s.db[service.COLLECTION].docs[0]
    row.update(status="failed", raw_response="stored response")
    async def denied(*args): return {"allowed": False, "message": "Quota exhausted"}
    monkeypatch.setattr(token_usage, "check_institute_token_budget", denied)
    response = await s.client.post(s.url, json={"retry_failed": True, "regenerate": True})
    assert response.status_code == 402
    assert row["raw_response"] == "stored response" and len(s.pending) == 1


async def test_incomplete_question_identity_fails_whole_job_but_retains_response(isolated, monkeypatch):
    s = isolated
    monkeypatch.setattr("app.db.mongodb.get_database", lambda: s.db)
    data = answer()
    data["questions"].reverse()
    async def tracked(*args, **kwargs): return json.dumps(data), {}
    monkeypatch.setattr(service, "tracked_faculty_call", tracked)
    await s.client.post(s.url, json={})
    _, job_id, attempt, ctx = s.pending[0]
    await service.run_suggestion_job(job_id, attempt, ctx)
    row = s.db[service.COLLECTION].docs[0]
    assert row["status"] == "failed" and row["raw_response"]
    assert "result" not in row
