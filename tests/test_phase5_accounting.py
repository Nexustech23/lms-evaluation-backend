"""No billable requests: ledger replay, recovery, cancellation and attempt checks."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import pytest
from bson import ObjectId

from app.services import gemini, claude
from app.services import grading as grading_service
from app.services.ai_usage import record_ai_usage
from app.services.ai_usage_rollups import reconcile_usage_rollups
from app.utils.ai_pricing import estimate_cost_usd
from tests.test_failed_ai_cost import accounting, call, get


async def record(s, event_id, usage=None, model="claude-sonnet-4-6"):
    await record_ai_usage(s.db, user_id=s.user, provider="claude", model=model,
        feature="qp_generation", usage=usage if usage is not None else {"input_tokens": 100, "output_tokens": 50},
        institute_id=str(s.institute), event_id=event_id, record_institute=True)


@pytest.mark.parametrize("metadata", [None, SimpleNamespace(), SimpleNamespace(prompt_token_count=0)])
async def test_missing_gemini_metadata_stays_unknown(accounting, metadata):
    await call(accounting, lambda: ("text", gemini._parse_token_usage(metadata)), "gemini")
    event = accounting.db["aiUsageEvents"].docs[0]
    assert event["usage_status"] == event["cost_status"] == "unknown"
    assert event["cost_usd"] is None and event["total_tokens"] is None
    assert event["call_status"] == "completed"


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": None, "output_tokens": None},
    {"input_tokens": "bad", "output_tokens": 2}, {"input_tokens": -1, "output_tokens": 2},
    {"input_tokens": True, "output_tokens": 2}, {"input_tokens": 0.5, "output_tokens": 2},
    {"input_tokens": "NaN", "output_tokens": 2}])
async def test_unavailable_or_invalid_usage_is_not_free(accounting, usage):
    await record_ai_usage(accounting.db, user_id=accounting.user, provider="claude", model="claude-sonnet-4-6",
                          feature="qp_generation", usage=usage, event_id=ObjectId())
    event = accounting.db["aiUsageEvents"].docs[0]
    assert event["usage_status"] == "unknown"
    assert event["cost_usd"] is None


async def test_unpriced_model_preserves_tokens_and_unknown_cost(accounting):
    await record(accounting, ObjectId(), model="unpriced-model")
    event = accounting.db["aiUsageEvents"].docs[0]
    assert event["usage_status"] == "known"
    assert event["cost_status"] == "unknown" and event["cost_usd"] is None
    assert event["total_tokens"] == 150
    assert get(accounting.db["instituteDetails"].docs[0], "token_usage.claude.total_tokens") == 150


async def test_real_zero_response_is_known_zero(accounting):
    await record(accounting, ObjectId(), {"input_tokens": 0, "output_tokens": 0})
    event = accounting.db["aiUsageEvents"].docs[0]
    assert event["usage_status"] == "known" and event["cost_usd"] == 0


@pytest.mark.parametrize("cached_key", ["cache_read_input_tokens", "cache_creation_input_tokens"])
async def test_cache_tokens_retained_without_invented_price(accounting, cached_key):
    await record(accounting, ObjectId(), {"input_tokens": 0, "output_tokens": 0, cached_key: 100})
    event = accounting.db["aiUsageEvents"].docs[0]
    assert event["total_tokens"] == event["cache_tokens"] == 100
    assert event["cost_status"] == "unknown" and event["cost_usd"] is None


async def test_replay_is_idempotent_and_cannot_replace_known_usage(accounting):
    event_id = ObjectId()
    await record(accounting, event_id)
    cost = accounting.db["aiUsageEvents"].docs[0]["cost_usd"]
    for _ in range(3):
        await record(accounting, event_id, {"input_tokens": 999, "output_tokens": 999})
    assert len(accounting.db["aiUsageEvents"].docs) == 1
    assert accounting.db["aiUsageEvents"].docs[0]["cost_usd"] == cost
    assert get(accounting.db["users"].docs[0], "token_usage.claude.input_tokens") == 100
    assert get(accounting.db["instituteDetails"].docs[0], "token_usage.claude.total_tokens") == 150
    assert get(accounting.db["instituteDetails"].docs[0], "token_usage.claude.call_count") == 1


@pytest.mark.parametrize("target", ["users", "instituteDetails"])
async def test_partial_projection_failure_repaired_without_double_count(accounting, monkeypatch, target):
    original = accounting.db[target].update_one
    monkeypatch.setattr(accounting.db[target], "update_one", AsyncMock(side_effect=RuntimeError("DB write failed")))
    await record(accounting, ObjectId())
    event = accounting.db["aiUsageEvents"].docs[0]
    assert event["rollup_pending"] is True and event["cost_usd"] > 0
    monkeypatch.setattr(accounting.db[target], "update_one", original)
    await reconcile_usage_rollups(accounting.db)
    await reconcile_usage_rollups(accounting.db)
    assert event["rollup_pending"] is False
    assert get(accounting.db["users"].docs[0], "token_usage.claude.input_tokens") == 100
    assert get(accounting.db["instituteDetails"].docs[0], "token_usage.grand_total_tokens") == 150


async def test_legacy_baseline_is_preserved_once(accounting):
    accounting.db["users"].docs[0]["token_usage"] = {"claude": {"input_tokens": 40, "output_tokens": 10}}
    accounting.db["instituteDetails"].docs[0]["token_usage"] = {
        "claude": {"total_tokens": 50, "total_input_tokens": 40, "total_output_tokens": 10, "call_count": 2},
        "grand_total_tokens": 50, "evaluation_count": 9}
    event_id = ObjectId()
    await record(accounting, event_id)
    await record(accounting, event_id)
    await record(accounting, ObjectId())
    assert get(accounting.db["users"].docs[0], "token_usage.claude.input_tokens") == 240
    institute = accounting.db["instituteDetails"].docs[0]
    assert get(institute, "token_usage.grand_total_tokens") == 350
    assert get(institute, "token_usage.claude.call_count") == 4
    assert get(institute, "token_usage.evaluation_count") == 9


async def test_stale_concurrent_projection_cannot_roll_back_counts(accounting, monkeypatch):
    target = accounting.db["users"]
    original = target.update_one
    paused, release = asyncio.Event(), asyncio.Event()
    first = True
    async def delayed(query, update, **kwargs):
        nonlocal first
        if "$max" in update and first:
            first = False
            paused.set()
            await release.wait()
        return await original(query, update, **kwargs)
    monkeypatch.setattr(target, "update_one", delayed)
    task = asyncio.create_task(record(accounting, ObjectId()))
    await asyncio.wait_for(paused.wait(), 3)
    await record(accounting, ObjectId())
    release.set()
    await task
    assert get(target.docs[0], "token_usage.claude.input_tokens") == 200
    assert get(accounting.db["instituteDetails"].docs[0], "token_usage.claude.total_tokens") == 300


async def test_cancelled_caller_does_not_discard_thread_result(accounting):
    started, release = threading.Event(), threading.Event()
    def provider():
        started.set()
        release.wait(5)
        return "done", {"input_tokens": 100, "output_tokens": 50}
    task = asyncio.create_task(call(accounting, provider))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    for _ in range(200):
        if accounting.db["aiUsageEvents"].docs[0].get("finalized"):
            break
        await asyncio.sleep(0.005)
    event = accounting.db["aiUsageEvents"].docs[0]
    assert event["caller_cancelled"] is True
    assert event["usage_status"] == "known" and event["total_tokens"] == 150
    assert event["cost_usd"] > 0


@pytest.mark.parametrize("failures", [0, 1, 4, 5])
async def test_each_visible_gemini_retry_has_its_own_event(accounting, monkeypatch, failures):
    response = SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=100,
        candidates_token_count=20, total_token_count=150), text="answer", candidates=[])
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(
        side_effect=[RuntimeError("503 UNAVAILABLE")] * failures + [response])))
    monkeypatch.setattr(gemini, "_get_client", lambda: client)
    monkeypatch.setattr(gemini.time, "sleep", lambda _: None)
    if failures == 5:
        with pytest.raises(RuntimeError):
            await call(accounting, lambda: gemini.generate_content_from_file(b"pdf", "application/pdf", "prompt"), "gemini")
    else:
        await call(accounting, lambda: gemini.generate_content_from_file(b"pdf", "application/pdf", "prompt"), "gemini")
    events = accounting.db["aiUsageEvents"].docs
    assert len(events) == min(failures + 1, 5)
    assert sum(e["usage_status"] == "unknown" for e in events) == failures
    assert client.models.generate_content.call_count == min(failures + 1, 5)


async def test_nonretryable_error_records_only_one_attempt(accounting, monkeypatch):
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=RuntimeError("400 bad input"))))
    monkeypatch.setattr(gemini, "_get_client", lambda: client)
    with pytest.raises(RuntimeError):
        await call(accounting, lambda: gemini.generate_content_from_file(b"pdf", "application/pdf", "prompt"), "gemini")
    assert len(accounting.db["aiUsageEvents"].docs) == 1


def test_shared_claude_sdk_does_not_make_hidden_retries(monkeypatch):
    monkeypatch.setattr(claude, "_client", None)
    factory = Mock()
    monkeypatch.setattr(claude.anthropic, "Anthropic", factory)
    claude._get_client()
    assert factory.call_args.kwargs["max_retries"] == 0


def test_tiny_known_spend_is_not_rounded_to_zero():
    assert 0 < estimate_cost_usd("gemini-3.1-flash-lite", 1, 0) < 0.000001


async def test_grading_wrapper_preserves_unknown_gemini_metadata(accounting, monkeypatch):
    monkeypatch.setattr(grading_service, "generate_content_from_file", Mock(return_value=("OCR", gemini._parse_token_usage(None))))
    await call(accounting, lambda: grading_service.extract_answer_text_with_gemini(b"pdf"), "gemini")
    assert accounting.db["aiUsageEvents"].docs[0]["usage_status"] == "unknown"


@pytest.mark.parametrize("kind", ["grading", "transcript"])
async def test_claude_wrappers_preserve_cache_usage(accounting, monkeypatch, kind):
    monkeypatch.setattr(grading_service, "generate_text", Mock(return_value=("text", {
        "input_tokens": 20, "output_tokens": 10, "total_tokens": 30, "cache_read_input_tokens": 100})))
    function = (lambda: grading_service.grade_with_claude("q", "a", [{"maxMarks": 10}])) if kind == "grading" else (
        lambda: grading_service.generate_transcript_html_with_claude("answer"))
    await call(accounting, function)
    event = accounting.db["aiUsageEvents"].docs[0]
    assert event["total_tokens"] == 130 and event["cache_tokens"] == 100
    assert event["cost_status"] == "unknown"


@pytest.mark.parametrize("ambiguous_commit", [False, True])
async def test_transient_ledger_write_retries_with_one_stable_id(accounting, monkeypatch, ambiguous_commit):
    collection = accounting.db["aiUsageEvents"]
    original = collection.update_one
    first = True
    async def transient(query, changes, **kwargs):
        nonlocal first
        if first and "$setOnInsert" in changes:
            first = False
            if ambiguous_commit:
                await original(query, changes, **kwargs)
            raise RuntimeError("Write result unavailable")
        return await original(query, changes, **kwargs)
    monkeypatch.setattr(collection, "update_one", transient)
    await record_ai_usage(accounting.db, user_id=accounting.user, provider="claude", model="claude-sonnet-4-6",
                          feature="qp_generation", usage={"input_tokens": 100, "output_tokens": 50},
                          institute_id=str(accounting.institute), record_institute=True)
    assert len(collection.docs) == 1
    assert get(accounting.db["instituteDetails"].docs[0], "token_usage.claude.total_tokens") == 150
