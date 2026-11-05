"""Paper generation: a wrapped reply is still read, a dropped connection is retried, the cap fits a long paper."""
import inspect
import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

import app.services.claude as claude
from app.api.routers import question_paper
from app.services.paper_blueprint import parse_paper_json, validate_paper

PAPER = {"sections": [{"label": "Section A", "attempt_count": 1, "questions": [
    {"number": 1, "text": "What is a token?", "marks": 2, "bloom_level": 2, "co_codes": [], "diagrams": []}]}]}
SECTIONS = [{"label": "Section A", "percent": 100, "bloomLevels": []}]
BARE = json.dumps(PAPER)


@pytest.mark.parametrize("reply", [
    BARE,
    "```json\n" + BARE + "\n```",
    "```\n" + BARE + "\n```",
    "Here is the paper:\n" + BARE,
    BARE + "\nLet me know if you want changes.",
    "Here is the paper:\n```json\n" + BARE + "\n```\nHope this helps.",
])
def test_a_reply_with_a_wrapper_around_the_json_is_read(reply):
    assert parse_paper_json(reply) == PAPER


@pytest.mark.parametrize("reply", ["", "   ", "I cannot write this paper.", "```json\nnot json\n```", None])
def test_a_reply_without_paper_data_still_fails_as_before(reply):
    with pytest.raises(json.JSONDecodeError):
        parse_paper_json(reply)


def test_a_wrapped_reply_passes_the_full_validation():
    text, blueprint = validate_paper("```json\n" + BARE + "\n```", SECTIONS, 2, [])
    assert "Q1. What is a token? [2 Marks]" in text and blueprint["required_marks"] == 2


def test_a_wrapped_reply_with_wrong_marks_is_still_rejected():
    bad = json.loads(BARE)
    bad["sections"][0]["questions"][0]["marks"] = 5
    with pytest.raises(ValueError):
        validate_paper("Here you go:\n" + json.dumps(bad), SECTIONS, 2, [])


def test_the_log_records_the_shape_of_an_unreadable_reply_not_its_words(monkeypatch):
    from app.services import paper_blueprint
    events = []
    monkeypatch.setattr(paper_blueprint, "event", lambda name, **fields: events.append((name, fields)))
    with pytest.raises(json.JSONDecodeError):
        parse_paper_json("Secret syllabus words {not json")
    name, fields = events[0]
    assert name == "paper.reply_unreadable"
    assert fields["first_char_kind"] == "letter" and fields["first_brace_index"] > 0
    assert "Secret" not in json.dumps(fields)


def test_the_paper_cap_is_raised_and_used_for_the_paper_call():
    assert question_paper.PAPER_MAX_OUTPUT_TOKENS == 30000
    assert "PAPER_MAX_OUTPUT_TOKENS" in inspect.getsource(question_paper._run_generation_job)


# ---- connection retry ----

REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
OK = SimpleNamespace(content=[SimpleNamespace(type="text", text="hello")], stop_reason="end_turn",
                     usage=SimpleNamespace(input_tokens=3, output_tokens=2))


def connect_error():
    try:
        try:
            raise httpx.ConnectError("[Errno 101] Network is unreachable")
        except httpx.ConnectError as cause:
            raise anthropic.APIConnectionError(request=REQUEST) from cause
    except anthropic.APIConnectionError as error:
        return error


def timeout_error():
    try:
        try:
            raise httpx.ReadTimeout("slow")
        except httpx.ReadTimeout as cause:
            raise anthropic.APITimeoutError(request=REQUEST) from cause
    except anthropic.APITimeoutError as error:
        return error


def fake_client(monkeypatch, outcomes):
    calls, sleeps = [], []

    def create(**request):
        calls.append(request)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(claude, "_get_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setattr(claude.time, "sleep", sleeps.append)
    return calls, sleeps


def test_a_connection_that_could_not_be_made_is_retried_and_then_succeeds(monkeypatch):
    calls, sleeps = fake_client(monkeypatch, [connect_error(), connect_error(), OK])
    text, usage = claude.generate_text("prompt")
    assert text == "hello" and len(calls) == 3 and len(sleeps) == 2 and usage["total_tokens"] == 5


def test_the_retry_gives_up_after_two_repeats(monkeypatch):
    calls, _ = fake_client(monkeypatch, [connect_error()] * 3)
    with pytest.raises(RuntimeError, match="Connection error"):
        claude.generate_text("prompt")
    assert len(calls) == 3


def test_a_timeout_is_not_retried_because_it_may_already_be_billed(monkeypatch):
    calls, sleeps = fake_client(monkeypatch, [timeout_error(), OK])
    with pytest.raises(RuntimeError):
        claude.generate_text("prompt")
    assert len(calls) == 1 and sleeps == []


def test_an_answer_cut_off_at_the_cap_is_still_rejected_without_a_retry(monkeypatch):
    cut = SimpleNamespace(content=[SimpleNamespace(type="text", text="{")], stop_reason="max_tokens",
                          usage=SimpleNamespace(input_tokens=3, output_tokens=8000))
    calls, _ = fake_client(monkeypatch, [cut, OK])
    with pytest.raises(RuntimeError, match="incomplete"):
        claude.generate_text("prompt")
    assert len(calls) == 1


def test_the_paper_is_written_by_sonnet_5_5_and_billed_at_its_price():
    from app.utils.ai_pricing import _PRICES as PRICES
    assert question_paper.PAPER_MODEL == "claude-sonnet-5-5"
    source = inspect.getsource(question_paper._run_generation_job)
    assert source.count("PAPER_MODEL") >= 4 and "claude-sonnet-4-6" not in source  # the paper and its check both use it
    assert "claude-sonnet-5-5" in PRICES
