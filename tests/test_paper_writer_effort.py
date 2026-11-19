"""The paper is written at a configured thinking effort, and the log carries the API's own thinking-token count.
No AI call, no database, no network: fakes answer."""
import functools
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.config import settings
from app.services.claude import content_breakdown
from tests.test_paper_timing import job_params, patch_job, physics_reply, run


def test_the_default_effort_is_medium():
    assert type(settings).model_fields["PAPER_WRITER_EFFORT"].default == "medium"


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_the_writer_passes_the_configured_effort(monkeypatch, effort):
    from app.api.routers import question_paper as qp
    fake = Mock(return_value=("text", {}))
    monkeypatch.setattr(qp, "generate_text", fake)
    monkeypatch.setattr(settings, "PAPER_WRITER_EFFORT", effort)
    qp._paper_writer()("prompt", "model", 100)
    fake.assert_called_once_with("prompt", "model", 100, effort=effort)


def test_default_sends_no_effort_at_all(monkeypatch):
    from app.api.routers import question_paper as qp
    fake = Mock(return_value=("text", {}))
    monkeypatch.setattr(qp, "generate_text", fake)
    monkeypatch.setattr(settings, "PAPER_WRITER_EFFORT", "default")
    qp._paper_writer()("prompt", "model", 100)
    fake.assert_called_once_with("prompt", "model", 100)


def test_the_single_call_job_uses_the_writer_and_the_answer_check_keeps_its_own_effort(monkeypatch):
    reply, blueprint = physics_reply()
    qp, db, patches = patch_job(monkeypatch, reply)
    seen = []
    inner = qp.tracked_faculty_call

    async def recorder(database, function, *args, feature=None, **kwargs):
        seen.append((feature, function, args))
        return await inner(database, function, *args, feature=feature, **kwargs)

    monkeypatch.setattr(qp, "tracked_faculty_call", recorder)
    monkeypatch.setattr(settings, "PAPER_WRITER_EFFORT", "low")
    run(qp._run_generation_job("job1", job_params(blueprint), {}))
    writer = next(function for feature, function, _ in seen if feature == "qp_generation")
    assert isinstance(writer, functools.partial) and writer.keywords == {"effort": "low"}
    check = next((function, args) for feature, function, args in seen if feature == "qp_answer_check")
    assert check[0] is qp.check_paper and check[1][-1] == qp.ANSWER_CHECK_EFFORT == "medium"


def test_the_single_call_job_with_default_effort_sends_the_plain_function(monkeypatch):
    reply, blueprint = physics_reply()
    qp, db, patches = patch_job(monkeypatch, reply)
    seen = []
    inner = qp.tracked_faculty_call

    async def recorder(database, function, *args, feature=None, **kwargs):
        seen.append((feature, function))
        return await inner(database, function, *args, feature=feature, **kwargs)

    monkeypatch.setattr(qp, "tracked_faculty_call", recorder)
    monkeypatch.setattr(settings, "PAPER_WRITER_EFFORT", "default")
    run(qp._run_generation_job("job1", job_params(blueprint), {}))
    assert next(function for feature, function in seen if feature == "qp_generation") is qp.generate_text


# ---- the API's own thinking-token count --------------------------------------------------------------------------------

def message(usage, content=()):
    return SimpleNamespace(content=list(content), stop_reason="end_turn", usage=usage)


def test_thinking_tokens_are_read_from_an_attribute():
    usage = SimpleNamespace(output_tokens_details=SimpleNamespace(thinking_tokens=1234))
    assert content_breakdown(message(usage))["thinking_tokens"] == 1234


def test_thinking_tokens_are_read_from_a_dict():
    usage = SimpleNamespace(output_tokens_details={"thinking_tokens": 99})
    assert content_breakdown(message(usage))["thinking_tokens"] == 99


def test_thinking_tokens_are_read_from_an_older_sdks_extra_fields():
    usage = SimpleNamespace(model_extra={"output_tokens_details": {"thinking_tokens": 7}})
    assert content_breakdown(message(usage))["thinking_tokens"] == 7


def test_a_response_without_the_count_gives_none_and_still_a_breakdown():
    result = content_breakdown(message(SimpleNamespace()))
    assert result["thinking_tokens"] is None and result["stop_reason"] == "end_turn"
