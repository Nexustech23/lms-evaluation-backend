import json

import pytest

from app.services import rubric_suggestions as service
from app.services.rubric_marks import quoted_maximum_matches
from tests.test_rubric_suggestions import answer, context


@pytest.mark.parametrize("notation", ["[2 Marks]", "(2 marks)", "Marks: 2", "Marks = 2", "[2]", "(2)"])
def test_common_printed_maxima_are_supported(notation):
    ctx = context()
    ctx["questions"][0]["text"] = f"Q1. Define learning. {notation} [CO1]"
    data = answer()
    data["questions"][0]["max_quote"] = notation
    result = service.validate_suggestions(json.dumps(data), ctx)
    assert result["questions"][0]["status"] == "draft_ready"


def test_table_marks_need_the_marks_column_not_another_number():
    paper = "| Q | Question | Marks | CO |\n| 1 | Give 10 examples | 2 | CO1 |"
    block = {"text": paper.splitlines()[1], "start": paper.index("| 1 |")}
    assert quoted_maximum_matches(2, "2", block, {"paper_text": paper})
    assert not quoted_maximum_matches(10, "10", block, {"paper_text": paper})
    assert not quoted_maximum_matches(1, "1", block, {"paper_text": paper})


@pytest.mark.parametrize("quote,text", [
    ("2", "Use 2 examples."),
    ("[2]", "Use formula [2] to calculate the answer."),
    ("Minimum 2 Marks", "Minimum 2 Marks"),
    ("Pass Marks: 2", "Pass Marks: 2"),
])
def test_incidental_numbers_and_minima_are_not_maxima(quote, text):
    assert not quoted_maximum_matches(2, quote, {"text": text}, {"paper_text": text})


def test_one_bad_quote_does_not_discard_other_questions_or_apply_wrong_marks():
    data = answer()
    data["questions"][0].update(max_marks=9)
    result = service.validate_suggestions(json.dumps(data), context())
    bad, good = result["questions"]
    assert result["review_count"] == 1
    assert bad["status"] == "needs_clarification" and bad["max_marks"] is None
    assert bad["parameters"] == [] and bad["guidelines"] == ""
    assert bad["review_draft"]["max_marks"] == 9
    assert "Question Q1" in bad["notes"][0] and "9" in bad["notes"][0]
    assert good["status"] == "draft_ready" and good["max_marks"] == 3


def test_invalid_boolean_or_out_of_range_values_are_not_reviewable_numbers():
    data = answer()
    data["questions"][0]["parameters"][0]["percentage"] = True
    result = service.validate_suggestions(json.dumps(data), context())
    row = result["questions"][0]
    assert row["status"] == "needs_clarification"
    assert "review_draft" not in row
    assert "parameters.0.percentage" in row["notes"][0]


def test_nonfinite_output_and_changed_question_identity_still_fail_closed():
    data = answer()
    data["questions"][0]["max_marks"] = float("nan")
    with pytest.raises(ValueError):
        service.validate_suggestions(json.dumps(data), context())
    data = answer()
    data["questions"][0]["question_no"] = True
    with pytest.raises(ValueError):
        service.validate_suggestions(json.dumps(data), context())
