"""Offline provider budgeting and resumable rubric batch contracts."""
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import rubric_suggestions as service
from tests.test_rubric_suggestions import isolated, answer, context


def paper_context(count):
    text = "INSTRUCTIONS: Answer all.\n" + "\n".join(f"Q{n}. Define concept {n}. [2 Marks]" for n in range(1, count + 1))
    return {"questions": service.question_blocks(text, count), "paper_text": text,
            "available_cos": [{"co_code": "CO1", "description": "Absent from this paper"}]}


def batch_answer(ctx):
    template = answer()["questions"][0]
    return json.dumps({"questions": [{**deepcopy(template), "question_no": q["question_no"], "co_tags": []}
                                     for q in ctx["questions"]]})


def test_batches_are_bounded_and_keep_global_question_ids():
    ctx = paper_context(38)
    batches = list(service.generation_batches(ctx))
    assert [len(batch["questions"]) for _, batch in batches] == [6, 6, 6, 6, 6, 6, 2]
    assert [q["question_no"] for _, batch in batches for q in batch["questions"]] == list(range(1, 39))
    assert all(batch["paper_instructions"] == "INSTRUCTIONS: Answer all.\n" for _, batch in batches)
    assert all(batch["available_cos"] == [] for _, batch in batches)
    assert batches[1][0] == "7-8-9-10-11-12"
    assert len({key for key, _ in batches}) == 7


def test_batch_payload_omits_other_questions_and_absent_co_definitions(monkeypatch):
    ctx = paper_context(8)
    batch = list(service.generation_batches(ctx))[1][1]
    provider = Mock()
    provider.messages.create.return_value = SimpleNamespace(stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=batch_answer(batch))], usage=SimpleNamespace(input_tokens=5, output_tokens=8))
    monkeypatch.setattr(service, "_get_client", lambda: provider)
    service.generate_suggestions(batch)
    sent = provider.messages.create.call_args.kwargs
    payload = json.loads(sent["messages"][0]["content"])
    assert sent["max_tokens"] == 30000
    assert sent["extra_body"]["output_config"]["effort"] == "medium"
    assert payload["available_cos"] == []
    assert all(q["extract_co_tags"] is False for q in payload["questions"])
    assert [q["question_no"] for q in payload["questions"]] == [7, 8]
    assert "Q1." not in payload["paper_instructions"]


def test_only_printed_batch_cos_are_supplied():
    ctx = paper_context(7)
    ctx["questions"][-1]["text"] += " [CO2]"
    ctx["available_cos"].append({"co_code": "CO2", "description": "Printed"})
    batches = list(service.generation_batches(ctx))
    assert batches[0][1]["available_cos"] == []
    assert batches[1][1]["available_cos"] == [{"co_code": "CO2", "description": "Printed"}]


@pytest.mark.parametrize("stop", ["max_tokens", "refusal"])
def test_incomplete_batch_keeps_stop_reason_partial_text_and_usage(monkeypatch, stop):
    provider = Mock()
    provider.messages.create.return_value = SimpleNamespace(stop_reason=stop,
        content=[SimpleNamespace(type="text", text='{"questions":[')], usage=SimpleNamespace(input_tokens=5, output_tokens=30000))
    monkeypatch.setattr(service, "_get_client", lambda: provider)
    with pytest.raises(RuntimeError) as error:
        service.generate_suggestions(context())
    assert error.value.stop_reason == stop
    assert error.value.partial_response == '{"questions":['
    assert error.value.usage["output_tokens"] == 30000
    assert provider.messages.create.call_count == 1


async def test_retry_reuses_completed_batches_and_calls_only_unfinished(isolated, monkeypatch):
    s = isolated
    ctx = paper_context(8)
    s.db["newsavedDocs"].docs[0]["question_paper"].update(text=ctx["paper_text"], no_of_questions=8)
    monkeypatch.setattr("app.db.mongodb.get_database", lambda: s.db)
    calls = []
    fail_second = True
    async def tracked(db, function, batch, **kwargs):
        numbers = [q["question_no"] for q in batch["questions"]]
        calls.append(numbers)
        if fail_second and numbers[0] == 7:
            error = RuntimeError("Output cap")
            error.stop_reason = "max_tokens"
            error.partial_response = '{"questions":['
            raise error
        return batch_answer(batch), {}
    monkeypatch.setattr(service, "tracked_faculty_call", tracked)
    await s.client.post(s.url, json={})
    _, job_id, attempt, full_ctx = s.pending[0]
    await service.run_suggestion_job(job_id, attempt, full_ctx)
    row = s.db[service.COLLECTION].docs[0]
    assert row["status"] == "failed" and row["completed_questions"] == 6
    assert "30,000-token" in row["error"]
    assert row["incomplete_responses"]["7-8"]["stop_reason"] == "max_tokens"
    assert "1-2-3-4-5-6" in row["batch_responses"]
    assert calls == [[1, 2, 3, 4, 5, 6], [7, 8]]
    assert "raw_response" not in row
    fail_second = False
    await s.client.post(s.url, json={"retry_failed": True})
    _, job_id, attempt, full_ctx = s.pending[-1]
    await service.run_suggestion_job(job_id, attempt, full_ctx)
    assert row["status"] == "completed"
    assert calls == [[1, 2, 3, 4, 5, 6], [7, 8], [7, 8]]
    assert [q["question_no"] for q in row["result"]["questions"]] == list(range(1, 9))
    assert row["completed_questions"] == 8


async def test_fully_cached_batches_can_revalidate_without_ai_key_or_budget(isolated, monkeypatch):
    from app.core.config import settings
    from app.utils import token_usage
    s = isolated
    ctx = paper_context(8)
    s.db["newsavedDocs"].docs[0]["question_paper"].update(text=ctx["paper_text"], no_of_questions=8)
    monkeypatch.setattr("app.db.mongodb.get_database", lambda: s.db)
    await s.client.post(s.url, json={})
    row = s.db[service.COLLECTION].docs[0]
    row.update(status="failed", batch_responses={key: batch_answer(batch) for key, batch in service.generation_batches(ctx)})
    async def forbidden(*args, **kwargs): raise AssertionError("Cached batch must not incur a new AI call or require credits")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(token_usage, "check_institute_token_budget", forbidden)
    monkeypatch.setattr(service, "tracked_faculty_call", forbidden)
    assert (await s.client.post(s.url, json={"retry_failed": True})).status_code == 200
    _, job_id, attempt, full_ctx = s.pending[-1]
    await service.run_suggestion_job(job_id, attempt, full_ctx)
    assert row["status"] == "completed"
    assert "batch_responses" not in (await s.client.get(s.url)).json()


async def test_source_change_between_batches_stops_before_next_paid_call(isolated, monkeypatch):
    s = isolated
    ctx = paper_context(8)
    paper = s.db["newsavedDocs"].docs[0]["question_paper"]
    paper.update(text=ctx["paper_text"], no_of_questions=8)
    monkeypatch.setattr("app.db.mongodb.get_database", lambda: s.db)
    calls = []
    async def tracked(db, function, batch, **kwargs):
        calls.append(batch)
        paper["text"] += " Changed"
        return batch_answer(batch), {}
    monkeypatch.setattr(service, "tracked_faculty_call", tracked)
    await s.client.post(s.url, json={})
    _, job_id, attempt, full_ctx = s.pending[-1]
    await service.run_suggestion_job(job_id, attempt, full_ctx)
    assert len(calls) == 1
    assert s.db[service.COLLECTION].docs[0]["status"] == "failed"


async def test_budget_exhaustion_between_batches_preserves_completed_work(isolated, monkeypatch):
    from app.utils import token_usage
    s = isolated
    ctx = paper_context(8)
    s.db["newsavedDocs"].docs[0]["question_paper"].update(text=ctx["paper_text"], no_of_questions=8)
    monkeypatch.setattr("app.db.mongodb.get_database", lambda: s.db)
    calls = []
    async def budget(*args): return {"allowed": not calls, "message": "Quota exhausted"}
    async def tracked(db, function, batch, **kwargs):
        calls.append(batch)
        return batch_answer(batch), {}
    monkeypatch.setattr(token_usage, "check_institute_token_budget", budget)
    monkeypatch.setattr(service, "tracked_faculty_call", tracked)
    await s.client.post(s.url, json={})
    _, job_id, attempt, full_ctx = s.pending[-1]
    await service.run_suggestion_job(job_id, attempt, full_ctx)
    row = s.db[service.COLLECTION].docs[0]
    assert len(calls) == 1 and row["status"] == "failed"
    assert row["completed_questions"] == 6 and row["batch_responses"]["1-2-3-4-5-6"]
