"""Offline checks for grading-model migration; all provider calls are mocked."""
import json
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import anthropic
import httpx
import pytest

from app.services import claude, grading
from app.utils.ai_pricing import estimate_cost_usd


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Live network calls are forbidden in these tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def message(stop_reason="end_turn", content=None):
    return SimpleNamespace(
        content=content if content is not None else [
            SimpleNamespace(type="thinking", thinking="private", signature="signature"),
            SimpleNamespace(type="text", text='{"questionwise_marking": []}'),
        ],
        usage=SimpleNamespace(input_tokens=100, output_tokens=70,
                              cache_read_input_tokens=20, cache_creation_input_tokens=0),
        stop_reason=stop_reason,
    )


def stream_client(monkeypatch, response):
    client = MagicMock()
    client.messages.stream.return_value.__enter__.return_value.get_final_message.return_value = response
    monkeypatch.setattr(claude, "_get_client", lambda: client)
    return client


def test_grading_requests_55_with_30000_and_explicit_effort(monkeypatch):
    provider = Mock(return_value=('{}', {"input_tokens": 100, "output_tokens": 70, "total_tokens": 170}))
    monkeypatch.setattr(grading, "generate_text", provider)
    _, usage = grading.grade_with_claude("Q1 {question}", "Q1 {answer}", [{"maxMarks": 5}])
    kwargs = provider.call_args.kwargs
    assert kwargs == {"model": "claude-sonnet-5-5", "max_tokens": 30000,
                      "system": grading.GRADING_POLICY, "effort": "high"}
    assert usage["model"] == "claude-sonnet-5-5"
    assert usage["output_tokens"] == 70


def test_grading_model_has_usage_cost_rate():
    assert estimate_cost_usd(grading.CLAUDE_GRADING_MODEL, 1000, 1000) == pytest.approx(0.012)


def test_transcript_keeps_45_and_18000(monkeypatch):
    provider = Mock(return_value=("<!DOCTYPE html><html></html>",
                                  {"input_tokens": 100, "output_tokens": 70, "total_tokens": 170}))
    monkeypatch.setattr(grading, "generate_text", provider)
    _, usage = grading.generate_transcript_html_with_claude("Q1: answer")
    assert provider.call_args.kwargs == {"model": "claude-sonnet-4-5", "max_tokens": 18000}
    assert usage["model"] == "claude-sonnet-4-5"


@pytest.mark.parametrize("known_usage", [False, True])
def test_transcript_failure_records_its_own_model(monkeypatch, known_usage):
    error = RuntimeError("offline provider failure")
    if known_usage:
        error.usage = {"input_tokens": 100, "output_tokens": 70, "total_tokens": 170}
    monkeypatch.setattr(grading, "generate_text", Mock(side_effect=error))
    html, usage = grading.generate_transcript_html_with_claude("Q1: answer")
    assert html
    assert usage["model"] == "claude-sonnet-4-5"
    assert usage["call_status"] == "failed"


def test_adaptive_stream_returns_only_text_and_keeps_usage(monkeypatch):
    client = stream_client(monkeypatch, message())
    text, usage = claude.generate_text("prompt", model="claude-sonnet-5-5", max_tokens=30000,
                                       system="policy", effort="high")
    client.messages.create.assert_not_called()
    request = client.messages.stream.call_args.kwargs
    assert request["extra_body"] == {"thinking": {"type": "adaptive"},
                                      "output_config": {"effort": "high"}}
    assert request["max_tokens"] == 30000
    assert request["system"] == "policy"
    assert text == '{"questionwise_marking": []}'
    assert usage["output_tokens"] == 70
    assert usage["cache_read_input_tokens"] == 20
    assert usage["total_tokens"] == 190


@pytest.mark.parametrize("reason", ["max_tokens", "refusal"])
def test_incomplete_stream_rejected_with_usage(monkeypatch, reason):
    stream_client(monkeypatch, message(stop_reason=reason))
    with pytest.raises(RuntimeError) as error:
        claude.generate_text("prompt", model="claude-sonnet-5-5", max_tokens=30000, effort="high")
    assert error.value.usage["output_tokens"] == 70


def test_thinking_without_text_is_rejected(monkeypatch):
    stream_client(monkeypatch, message(content=[SimpleNamespace(type="thinking", thinking="private")]))
    with pytest.raises(RuntimeError) as error:
        claude.generate_text("prompt", model="claude-sonnet-5-5", effort="high")
    assert error.value.usage["input_tokens"] == 100


def test_existing_callers_keep_create_without_new_options(monkeypatch):
    client = MagicMock()
    client.messages.create.return_value = message()
    monkeypatch.setattr(claude, "_get_client", lambda: client)
    claude.generate_text("prompt", model="claude-sonnet-4-5", max_tokens=18000)
    client.messages.stream.assert_not_called()
    assert client.messages.create.call_args.kwargs == {
        "model": "claude-sonnet-4-5", "max_tokens": 18000,
        "messages": [{"role": "user", "content": "prompt"}],
    }


def test_installed_sdk_forwards_new_fields_and_streams_without_network(monkeypatch):
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        events = [
            {"type": "message_start", "message": {
                "id": "msg_offline", "type": "message", "role": "assistant", "content": [],
                "model": "claude-sonnet-5-5", "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "{}"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 70}},
            {"type": "message_stop"},
        ]
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    with httpx.Client(transport=httpx.MockTransport(transport), trust_env=False) as http_client:
        with anthropic.Anthropic(api_key="offline-test-key", http_client=http_client,
                                 max_retries=0) as client:
            monkeypatch.setattr(claude, "_get_client", lambda: client)
            text, usage = claude.generate_text("prompt", model="claude-sonnet-5-5",
                                               max_tokens=30000, effort="high")
    assert len(requests) == 1
    assert requests[0]["model"] == "claude-sonnet-5-5"
    assert requests[0]["max_tokens"] == 30000
    assert requests[0]["thinking"] == {"type": "adaptive"}
    assert requests[0]["output_config"] == {"effort": "high"}
    assert requests[0]["stream"] is True
    assert text == "{}" and usage["output_tokens"] == 70
