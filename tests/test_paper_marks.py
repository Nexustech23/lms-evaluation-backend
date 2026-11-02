import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import rubric_suggestions as service
from app.services.paper_marks import apply_scheme, expected_marks, parse_max_marks, to_number
from app.services.question_mapping import resolve_questions
from tests.test_rubric_suggestions import answer, context

FIXTURES = Path(__file__).parent / "fixtures"


def paper(name):
    return (FIXTURES / name).read_text(encoding="utf8")


@pytest.mark.parametrize("token,value", [("5", 5), ("five", 5), ("Sixteen", 16), ("twenty-one", 21),
                                         ("2.5", Decimal("2.5")), ("ninety", 90)])
def test_numbers_and_number_words(token, value):
    assert to_number(token) == Decimal(value)


@pytest.mark.parametrize("token", ["", "many", "twenty-twenty", "ten-one", None])
def test_unreadable_numbers_are_none(token):
    assert to_number(token) is None


def test_maximum_marks_come_from_the_header():
    assert parse_max_marks("Time: 3h\nMaximum Marks: 70\n") == 70
    assert parse_max_marks("Max. Marks = 100") == 100
    assert parse_max_marks("Q1. Total marks of the class are 5" + " " * 4000 + "Maximum Marks: 9") is None


def test_physics_paper_resolves_every_question_including_awkward_layouts():
    text = paper("physics_sqp_70.txt")
    marks = expected_marks(resolve_questions(text, 33), text)
    assert sum(marks.values()) == 70
    expected = {**{n: 1 for n in range(1, 17)}, **{n: 2 for n in range(17, 22)},
                **{n: 3 for n in range(22, 29)}, 29: 4, 30: 4, 31: 5, 32: 5, 33: 5}
    assert marks == {n: Decimal(v) for n, v in expected.items()}


def test_maths_paper_resolves_from_question_ranges():
    text = paper("maths_sqp_80.txt")
    marks = expected_marks(resolve_questions(text, 38), text)
    assert sum(marks.values()) == 80
    expected = {**{n: 1 for n in range(1, 21)}, **{n: 2 for n in range(21, 26)},
                **{n: 3 for n in range(26, 32)}, **{n: 5 for n in range(32, 36)}, **{n: 4 for n in range(36, 39)}}
    assert marks == {n: Decimal(v) for n, v in expected.items()}


def small_paper(total=7, header="SECTION A (2 x 1 = 2)", extra=""):
    return (f"Maximum Marks: {total}\n{extra}\n{header}\n1. One? 1\n2. Two? 1\n"
            "SECTION B\n3. Three. 2+1\n4. Four. 2\n")


def test_heading_style_scheme_with_a_sum_adds_up():
    text = small_paper(extra="Section B contains two questions of two marks each")
    blocks = resolve_questions(text, 4)
    # A is 1+1, B is 2+2, which is 6, not the printed 7.
    assert expected_marks(blocks, text) is None
    text = small_paper(total=6, extra="Section B contains two questions of two marks each")
    assert expected_marks(resolve_questions(text, 4), text) == {1: 1, 2: 1, 3: 2, 4: 2}


@pytest.mark.parametrize("change", [
    lambda t: t.replace("Maximum Marks: 70", "Time Allowed: 3 hours"),                           # no printed total
    lambda t: t.replace("Section B contains five questions", "Section B contains four questions"),  # count mismatch
    lambda t: t.replace("of three marks each", "of four marks each"),                              # sum mismatch
    lambda t: t.replace("Section E contains three long answer questions of five marks each", "Section E contains three long answer questions"),  # uncovered
])
def test_inconsistent_or_incomplete_schemes_are_not_trusted(change):
    text = change(paper("physics_sqp_70.txt"))
    assert expected_marks(resolve_questions(text, 33), text) is None


def test_apply_scheme_leaves_blocks_untouched_when_untrusted():
    text = paper("physics_sqp_70.txt").replace("Maximum Marks: 70", "")
    blocks = resolve_questions(text, 33)
    assert not apply_scheme(blocks, text)
    assert all("resolved_max_marks" not in b for b in blocks)


def awkward():
    text = paper("physics_sqp_70.txt")
    blocks = resolve_questions(text, 33)
    assert apply_scheme(blocks, text)
    return text, blocks


def row_for(block, max_marks, quote=""):
    return {"question_no": block["question_no"], "status": "draft_ready", "max_marks": max_marks,
            "max_quote": quote, "min_marks": 0, "min_source": "default", "min_quote": "", "co_tags": [],
            "parameters": [{"name": "Correct content", "percentage": 100, "isCustom": True}],
            "guidelines": "Full credit for a complete correct answer; partial for valid incomplete work; zero otherwise.",
            "notes": []}


def test_scheme_marks_replace_printed_evidence_for_awkward_layouts():
    text, blocks = awkward()
    block = next(b for b in blocks if b["question_label"].lstrip("Q") == "25")
    assert block["resolved_max_marks"] == 3.0
    ctx = {"questions": [block], "paper_text": text, "available_cos": []}
    raw = json.dumps({"questions": [row_for(block, block["resolved_max_marks"], "2+1")]})
    row = service.validate_suggestions(raw, ctx)["questions"][0]
    assert row["status"] == "draft_ready" and row["max_marks"] == block["resolved_max_marks"]
    assert row["max_quote"] == "" and row["max_source"] == "paper_scheme"


def test_ai_disagreeing_with_the_scheme_goes_to_review_with_the_scheme_marks():
    text, blocks = awkward()
    block = blocks[26]
    ctx = {"questions": [block], "paper_text": text, "available_cos": []}
    wrong = block["resolved_max_marks"] + 1
    result = service.validate_suggestions(json.dumps({"questions": [row_for(block, wrong)]}), ctx)
    row = result["questions"][0]
    assert row["status"] == "needs_clarification" and row["parameters"] == []
    assert row["max_marks"] == block["resolved_max_marks"]
    assert row["review_draft"]["max_marks"] == wrong
    assert "marking scheme gives" in row["notes"][0]


def test_missing_ai_marks_are_filled_from_the_scheme():
    text, blocks = awkward()
    block = blocks[19]
    ctx = {"questions": [block], "paper_text": text, "available_cos": []}
    row = service.validate_suggestions(json.dumps({"questions": [row_for(block, None)]}), ctx)["questions"][0]
    assert row["status"] == "draft_ready" and row["max_marks"] == block["resolved_max_marks"]


def test_without_a_trusted_scheme_the_strict_check_still_applies():
    data = answer()
    data["questions"][0].update(max_marks=9, max_quote="[9 Marks]")
    result = service.validate_suggestions(json.dumps(data), context())
    assert result["questions"][0]["status"] == "needs_clarification"
    assert "max_source" not in result["questions"][1]
