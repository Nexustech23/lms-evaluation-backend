"""Choice groups suggested from a paper's own wording; no AI, nothing saved."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.api.routers import evaluation as router
from app.services import choice_groups as cg
from app.services.choice_suggestions import suggest_choice_groups

FIXTURES = Path(__file__).parent / "fixtures"


def paper(name):
    return (FIXTURES / name).read_text(encoding="utf8")


def groups(result):
    return [(g["questions"], g["answerN"]) for g in result["groups"]]


def test_pharmaceutical_analysis_groups_are_found_in_the_papers_wording():
    result = suggest_choice_groups(paper("pharma_analysis_75.txt"), 13)
    assert groups(result) == [([2, 3, 4], 2), ([5, 6, 7, 8, 9, 10, 11, 12, 13], 7)]
    assert [g["id"] for g in result["groups"]] == ["g1", "g2"] and result["notes"] == []
    assert result["groups"][0]["source"] == "Group B: answer any two"


def test_sub_items_inside_a_question_are_not_mistaken_for_questions():
    # Q8 lists "1.Normality", "2.Molarity", "3.Volume" inside Group C.
    result = suggest_choice_groups(paper("pharma_analysis_75.txt"), 13)
    assert 1 not in result["groups"][1]["questions"] and result["groups"][1]["questions"][0] == 5


def test_or_between_questions_is_reported_for_the_faculty_to_check():
    result = suggest_choice_groups(paper("optical_sensing_80.txt"), 12)
    assert groups(result) == [([2, 3, 4, 5, 6, 7, 8], 6), ([9, 10, 11, 12], 3)]
    assert len(result["notes"]) == 2 and all("OR" in note for note in result["notes"])


def test_answer_all_papers_and_any_n_over_the_parts_of_one_question_give_nothing():
    # Optical Group A is "1. Answer any 10 questions" but they are the parts of Q1, a single row.
    assert groups(suggest_choice_groups(paper("optical_sensing_80.txt"), 12))[0][0][0] == 2
    assert suggest_choice_groups(paper("environmental_80.txt"), 10) == {"groups": [], "notes": []}


@pytest.mark.parametrize("text", [None, "", "   "])
def test_missing_paper_text_is_explained_not_an_error(text):
    result = suggest_choice_groups(text)
    assert result["groups"] == [] and "not been extracted" in result["notes"][0]


def test_number_words_and_digits_both_work():
    text = "Group B\nAnswer any three questions.\n2) a\n3) b\n4) c\n5) d\n"
    assert groups(suggest_choice_groups(text)) == [([2, 3, 4, 5], 3)]
    assert groups(suggest_choice_groups(text.replace("three", "3"))) == [([2, 3, 4, 5], 3)]


def test_a_rubric_with_fewer_rows_than_the_paper_skips_the_group_and_says_so():
    result = suggest_choice_groups(paper("pharma_analysis_75.txt"), 10)
    assert groups(result) == [([2, 3, 4], 2)]
    assert any("rubric has 10 questions" in note for note in result["notes"])


def test_suggested_groups_are_valid_choice_groups_for_the_rubric():
    rows = [{"maxMarks": m} for m in (20, 10, 10, 10, *([5] * 9))]
    suggested = suggest_choice_groups(paper("pharma_analysis_75.txt"), 13)["groups"]
    valid = cg.validate_groups([{k: v for k, v in g.items() if k != "source"} for g in suggested], rows)
    assert cg.counted_max(rows, valid) == 75


def test_route_returns_the_suggestions_for_the_exams_paper(monkeypatch):
    exam = {"question_paper": {"text": paper("pharma_analysis_75.txt")}}
    monkeypatch.setattr(router, "require_exam_access", AsyncMock(return_value=exam))
    response = asyncio.run(router.choice_group_suggestions("exam-id", 13, identity={}, db=None))
    assert response["success"] is True and len(response["groups"]) == 2
