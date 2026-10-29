"""Offline pipeline contracts using authored responses, not model accuracy tests."""
import json
import socket
from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.services import grading, rubric_suggestions
from app.services.grading_validation import validate_grading
from app.services.mcq_grading import apply_deterministic_mcq_overrides


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline integration must not connect to external services")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def prepared_rubric():
    paper = "Q1. What does K represent in K-means? [2 Marks] [CO1]"
    context = {"paper_text": paper, "questions": rubric_suggestions.question_blocks(paper, 1),
               "available_cos": [{"co_code": "CO1", "description": "Clustering concepts"}]}
    draft = {"question_no": 1, "status": "draft_ready", "max_marks": 2,
             "max_quote": "[2 Marks]", "min_marks": 0, "min_source": "default", "min_quote": "",
             "co_tags": [{"code": "CO1", "quote": "[CO1]"}],
             "parameters": [{"name": "Meaning of K", "percentage": 100, "isCustom": True}],
             "guidelines": "Meaning of K: full 2 for number of clusters, including equivalent words; "
                           "partial 1 for a relevant incomplete concept; zero for incorrect or irrelevant meaning.",
             "notes": []}
    validated = rubric_suggestions.validate_suggestions(json.dumps({"questions": [draft]}), context)
    row = validated["questions"][0]
    # Explicit simulated faculty acceptance. Production application/save is not invoked.
    rules = [{"minMarks": row["min_marks"], "maxMarks": row["max_marks"],
              "parameters": row["parameters"], "guidelines": row["guidelines"],
              "cos": [{"co_code": "CO1", "marks": 2, "description": "Clustering concepts"}]}]
    return paper, rules


def response(score, text):
    return {"summary": {"total_ai_marks": 999}, "questionwise_marking": [{
        "question_no": 1, "max_marks": 2, "ai_awarded_marks": score, "final_marks": score,
        "parameters": [{"name": "Meaning of K", "weight_percentage": 100,
                        "ai_score": score, "remarks": "Authored test judgment"}],
        "cos": [{"co_code": "CO1", "ai_marks": score, "max_marks": 2, "remarks": "Concept credit"}],
        "reasoning": "Authored criterion-level judgment for pipeline verification.",
        "feedback": "Test feedback", "improvement": "", "evidence": [text] if score else [],
        "flags": {"unanswered": False, "incomplete": score == 1, "irrelevant": False,
                  "repetitive": False, "needs_review": False}}]}


@pytest.mark.parametrize("score,text", [(2, "K is the number of clusters."),
                                        (1, "K is a clustering setting."),
                                        (0, "K is the learning rate.")])
def test_reviewed_draft_reaches_grader_and_validated_scores_without_bonus(monkeypatch, score, text):
    paper, rules = prepared_rubric()
    before = deepcopy(rules)
    provider = Mock(return_value=(json.dumps(response(score, text)),
                                 {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20}))
    monkeypatch.setattr(grading, "generate_text", provider)
    raw, usage = grading.grade_with_claude(paper, text, rules)
    prompt = provider.call_args.args[0]
    assert json.dumps(grading.build_grading_rubric(rules)) in prompt
    assert json.dumps(text) in prompt
    assert provider.call_args.kwargs["system"] == grading.GRADING_POLICY
    result = validate_grading(json.loads(raw), rules, 2, text)
    assert result["summary"]["total_ai_marks"] == score  # Summary recalculated, not trusted.
    assert result["questionwise_marking"][0]["final_marks"] == score
    assert result["questionwise_marking"][0]["cos"][0]["ai_marks"] == score
    assert usage["total_tokens"] == 20
    assert rules == before


@pytest.mark.parametrize("fault", ["over_cap", "co_total", "parameter_total", "fabricated_quote"])
def test_pipeline_rejects_invalid_credit_or_allocations(fault):
    _, rules = prepared_rubric()
    text = "K is the number of clusters."
    result = response(2, text)
    row = result["questionwise_marking"][0]
    if fault == "over_cap": row["ai_awarded_marks"] = 2.5
    elif fault == "co_total": row["cos"][0]["ai_marks"] = 1
    elif fault == "parameter_total": row["parameters"][0]["ai_score"] = 1
    else: row["evidence"] = ["Invented student evidence"]
    with pytest.raises(ValueError):
        validate_grading(result, rules, 2, text)


@pytest.mark.parametrize("selection,expected", [("A", 0), ("B", 1)])
def test_deterministic_mcq_overrides_authored_ai_judgment(selection, expected):
    rules = [{"maxMarks": 1, "parameters": [{"name": "Answer-key correctness", "percentage": 100}],
              "cos": [{"co_code": "CO1", "marks": 1}]}]
    ai_row = response(0.5, "A")["questionwise_marking"][0]
    ai_row.update(max_marks=1, cos=[{"co_code": "CO1", "max_marks": 1, "ai_marks": 0.5}])
    rows, count = apply_deterministic_mcq_overrides([ai_row],
        {"1": {"answer": "B", "confident": True}}, {"1": selection})
    result = validate_grading({"questionwise_marking": rows}, rules, 1, selection, trusted_mcq={1})
    assert count == 1
    assert result["summary"]["total_ai_marks"] == expected
    assert result["questionwise_marking"][0]["cos"][0]["ai_marks"] == expected
