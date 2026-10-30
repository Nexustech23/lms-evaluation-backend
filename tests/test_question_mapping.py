"""Offline numbering, stable identity, and evidence-boundary regressions."""
import pytest
from fastapi import HTTPException

from app.services.question_mapping import candidates, mapped_blocks, resolve_questions, source_key, legacy_mcq_indices_match
from app.models.evaluation import validate_evaluation_details
from app.services.grading import build_grading_rubric


@pytest.mark.parametrize("paper,labels", [
    ("Q1. First [2 Marks]\nQ3. Third [4 Marks]", ["Q1", "Q3"]),
    ("Question 7: First\nQuestion 9: Second", ["Question 7", "Question 9"]),
    ("1(i) First [1 Mark]\n1(ii) Second [1 Mark]", ["1(i)", "1(ii)"]),
    ("8 (a) Define\n8 (b) Explain", ["8 (a)", "8 (b)"]),
    ("Q1(a) Define\nQ1(b) Explain", ["Q1(a)", "Q1(b)"]),
    ("| 1(i) | Define | 2 |\n| 1(ii) | Explain | 3 |", ["1(i)", "1(ii)"]),
    ("Q.1 First\nQ.2 Second", ["Q.1", "Q.2"]),
])
def test_printed_labels_are_not_required_to_be_consecutive(paper, labels):
    blocks = resolve_questions(paper, len(labels))
    assert [q["question_label"] for q in blocks] == labels
    assert [q["question_no"] for q in blocks] == list(range(1, len(labels) + 1))
    assert len({q["question_id"] for q in blocks}) == len(labels)
    assert blocks == resolve_questions(paper, len(labels))


def test_section_restart_keeps_distinct_ids_and_evidence():
    paper = "SECTION A\nQ1. First [2 Marks] [CO1]\nSECTION B\nQ1. Second [8 Marks] [CO4]"
    first, second = resolve_questions(paper, 2)
    assert first["question_label"] == second["question_label"] == "Q1"
    assert first["question_id"] != second["question_id"]
    assert first["section"] == "SECTION A" and second["section"] == "SECTION B"
    assert "CO4" not in first["text"] and "SECTION B" not in first["text"]


def test_subparts_can_be_grouped_without_losing_a_root():
    paper = "1(i) First [1 Mark]\n1(ii) Second [1 Mark]\n2(a) Third [3 Marks]\n3. Fourth [5 Marks]"
    blocks = resolve_questions(paper, 3)
    assert [q["question_label"] for q in blocks] == ["1", "2", "3"]
    assert "1(ii)" in blocks[0]["text"] and "2(a)" not in blocks[0]["text"]


@pytest.mark.parametrize("paper,count", [
    ("Q1. First\nQ1. Again", 2),
    ("Q1. First\nQuestion 1: Again", 2),
    ("Unnumbered question\nAnother task", 2),
    ("Instructions:\n1. Answer both\n2. Write neatly\n1. Define\n2. Explain", 2),
    ("Q1. Stem\nQ1(a) First\nQ1(b) Second", 3),
    ("Q1. First\nQ2. Second", 3),
])
def test_ambiguity_returns_review_options_not_a_numbering_requirement(paper, count):
    with pytest.raises(HTTPException) as exc:
        resolve_questions(paper, count)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "QUESTION_MAPPING_REQUIRED"
    assert exc.value.detail["lines"]


def test_faculty_can_map_unnumbered_source_by_exact_lines():
    paper = "Instructions\nDefine heat. [2 Marks]\nExplain work. [3 Marks]"
    selections = [{"start": paper.index("Define"), "question_label": "Heat"},
                  {"start": paper.index("Explain"), "question_label": "Work"}]
    blocks = resolve_questions(paper, 2, selections)
    assert blocks[0]["text"] == "Define heat. [2 Marks]"
    assert blocks[1]["question_label"] == "Work"


@pytest.mark.parametrize("selections", [
    [{"start": 0, "question_label": "1"}],
    [{"start": 0, "question_label": "1"}, {"start": 0, "question_label": "2"}],
    [{"start": 7, "question_label": "2"}, {"start": 0, "question_label": "1"}],
    [{"start": True, "question_label": "1"}, {"start": 7, "question_label": "2"}],
    [{"start": 1, "question_label": "1"}, {"start": 7, "question_label": "2"}],
    [{"start": 0, "question_label": ""}, {"start": 7, "question_label": "2"}],
])
def test_manual_mapping_cannot_reorder_duplicate_or_invent_boundaries(selections):
    with pytest.raises(HTTPException):
        mapped_blocks("Q1. A.\nQ2. B.", selections, 2)


def test_mapping_fingerprint_changes_with_source_or_count():
    paper = {"text": "Q1. First", "no_of_questions": 1}
    assert source_key(paper) != source_key({**paper, "text": "Q1. Changed"})
    assert source_key(paper) != source_key({**paper, "no_of_questions": 2})


def test_mapping_metadata_survives_validation_and_reaches_grading_prompt():
    block = resolve_questions("SECTION C\n8(a) Explain work. [5 Marks]", 1)[0]
    row = {"minMarks": 0, "maxMarks": 5, "parameters": [], "cos": [],
           "question_id": block["question_id"], "question_label": block["question_label"],
           "section": block["section"], "question_text": block["text"]}
    validated = validate_evaluation_details([row])
    assert validated[0]["question_id"] == block["question_id"]
    rubric = build_grading_rubric(validated)
    assert "Question 1:" in rubric and "8(a)" in rubric and "SECTION C" in rubric
    assert "Explain work" in rubric
    assert legacy_mcq_indices_match(validated) is False
    assert legacy_mcq_indices_match([{"question_label": "Q1"}, {"question_label": "Q2"}]) is True
    assert legacy_mcq_indices_match([{"question_label": "Q1"}, {"question_label": "Q1"}]) is False
    assert legacy_mcq_indices_match([{"maxMarks": 2}]) is True
