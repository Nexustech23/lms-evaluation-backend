"""A paper that offers a choice must not flag every question for review."""
import pathlib

import pytest

from app.services.evaluation_review import (
    UNREADABLE_REVIEW_REASON, answer_text_review_reason, flag_review, review_count)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_clean_answer_text_needs_no_review_even_for_a_paper_with_choices():
    # The paper text is not an input any more: choices alone never trigger a flag.
    assert answer_text_review_reason("ANSWER PAGE 1\nQ2 ... Q3 ...") is None


@pytest.mark.parametrize("text", ["[UNREADABLE]", "ANSWER PAGE 2\nx = [unreadable] y", "[UNREADABLE: extraction could not be confirmed complete]"])
def test_unreadable_text_still_flags_with_a_specific_reason(text):
    assert answer_text_review_reason(text) == UNREADABLE_REVIEW_REASON
    assert "optional" not in UNREADABLE_REVIEW_REASON.lower()


def test_extraction_issue_flags_even_without_the_marker():
    assert answer_text_review_reason("clean text", extraction_issue=True) == UNREADABLE_REVIEW_REASON


def test_missing_answer_text_is_not_a_reading_problem():
    assert answer_text_review_reason(None) is None
    assert answer_text_review_reason("") is None


def test_same_reason_is_not_listed_twice_on_a_question():
    question = {}
    flag_review(question, UNREADABLE_REVIEW_REASON)
    flag_review(question, UNREADABLE_REVIEW_REASON)
    assert question["review_reasons"] == [UNREADABLE_REVIEW_REASON]
    assert review_count([question, {}]) == 1


def test_grading_no_longer_uses_the_blanket_optional_policy_message():
    source = (ROOT / "app" / "api" / "routers" / "grading.py").read_text(encoding="utf8")
    assert "optional-question selection policy" not in source
    assert "choice_review_required" not in source
