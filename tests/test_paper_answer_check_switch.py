"""The answer check is off by default and can be brought back with a setting. No AI call, no database, no network."""
import pytest

from app.core.config import settings


def test_the_answer_check_is_off_by_default():
    assert type(settings).model_fields["PAPER_ANSWER_CHECK"].default is False


@pytest.mark.parametrize("switch,requested,expected", [
    (False, True, False),      # the faculty ticked the box, the server has it off
    (False, False, False),
    (True, True, True),        # both agree: the check runs
    (True, False, False),      # the server allows it, the faculty did not ask
])
def test_the_check_runs_only_when_asked_for_and_switched_on(monkeypatch, switch, requested, expected):
    from app.api.routers import question_paper as qp
    monkeypatch.setattr(settings, "PAPER_ANSWER_CHECK", switch)
    assert qp._answer_check_wanted(requested) is expected
