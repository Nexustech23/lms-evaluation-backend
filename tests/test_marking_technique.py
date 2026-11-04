"""Marking technique: step marking is added to the grading policy only when the exam asks for it."""
import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.schemas.evaluation import SaveEvaluationDetailsRequest
from app.services import grading
from app.services.grading import GRADING_POLICY, STEP_MARKING_POLICY, grading_system_policy

RULES = [{"minMarks": 0, "maxMarks": 2, "guidelines": "", "parameters": [], "cos": []}]


def squash(text):
    return " ".join(text.split())


def test_standard_and_unset_use_the_unchanged_base_policy():
    assert grading_system_policy() == GRADING_POLICY
    assert grading_system_policy(None) == GRADING_POLICY
    assert grading_system_policy("standard") == GRADING_POLICY


def test_step_marking_adds_the_technique_after_the_base_policy():
    policy = grading_system_policy("step_marking")
    assert policy.startswith(GRADING_POLICY) and policy.endswith(STEP_MARKING_POLICY)
    text = squash(policy)
    for rule in ("Mark by steps", "Method marks", "Error carried forward", "Relevant attempt",
                 "never more than half", "Never exceed the maximum", "more specific than this technique"):
        assert rule in text


def test_the_technique_says_it_is_the_approved_rule_that_allows_attempt_credit():
    text = squash(STEP_MARKING_POLICY)
    assert "approved marking rule" in text and "carried-forward" in text and "attempt credit" in text


def test_an_unknown_technique_is_refused():
    with pytest.raises(ValueError, match="marking technique"):
        grading_system_policy("generous")


@pytest.mark.parametrize("technique,expected_system", [
    (None, GRADING_POLICY), ("standard", GRADING_POLICY),
    ("step_marking", GRADING_POLICY + STEP_MARKING_POLICY)])
def test_the_grader_sends_the_policy_for_the_exam_technique(monkeypatch, technique, expected_system):
    provider = Mock(return_value=(json.dumps({}), {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}))
    monkeypatch.setattr(grading, "generate_text", provider)
    grading.grade_with_claude("Q1 paper", "answer", RULES, technique)
    assert provider.call_args.kwargs["system"] == expected_system


def test_an_unknown_technique_never_reaches_the_model(monkeypatch):
    provider = Mock()
    monkeypatch.setattr(grading, "generate_text", provider)
    text, usage = grading.grade_with_claude("Q1 paper", "answer", RULES, "generous")
    assert text.startswith("Claude evaluation error") and not provider.called


def test_rubric_save_accepts_only_the_two_techniques():
    body = {"questionEvaluationDetails": [{"maxMarks": 2}], "totalMarks": 2}
    assert SaveEvaluationDetailsRequest(**body).markingTechnique is None
    assert SaveEvaluationDetailsRequest(**body, markingTechnique="step_marking").markingTechnique == "step_marking"
    with pytest.raises(ValidationError):
        SaveEvaluationDetailsRequest(**body, markingTechnique="generous")
