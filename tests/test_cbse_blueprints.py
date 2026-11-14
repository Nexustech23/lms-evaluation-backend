"""CBSE board blueprints: the presets add up, and the validator enforces them. No AI calls are made."""
import json

import pytest

from app.services.cbse_blueprints import (
    BLUEPRINTS, STAGE_DEFAULTS, catalog, get_blueprint, pattern_count, pattern_marks, stage_for_class,
)
from app.services.paper_blueprint import allocate_sections, validate_paper


def _paper(blueprint, tweak=None):
    """A reply that follows the blueprint exactly: one valid question per slot."""
    n, sections = 0, []
    for sec in blueprint["sections"]:
        questions = []
        for marks, count in sec["pattern"]:
            for _ in range(count):
                n += 1
                questions.append({"number": n, "text": f"Question number {n} about topic {n}", "marks": marks,
                                  "bloom_level": 2, "co_codes": [], "diagrams": []})
        sections.append({"label": sec["label"], "attempt_count": len(questions), "questions": questions})
    if tweak:
        tweak(sections)
    return json.dumps({"sections": sections})


@pytest.mark.parametrize("key", list(BLUEPRINTS))
def test_every_blueprint_adds_up_to_its_total(key):
    bp = get_blueprint(key)
    assert sum(pattern_marks(s["pattern"]) for s in bp["sections"]) == bp["total"]
    assert allocate_sections(bp["sections"], bp["total"]) == [pattern_marks(s["pattern"]) for s in bp["sections"]]


def test_question_counts_match_the_cbse_sample_papers():
    counts = {k: sum(pattern_count(s["pattern"]) for s in get_blueprint(k)["sections"]) for k in BLUEPRINTS}
    assert counts == {"X-MATH-STD": 38, "X-MATH-BASIC": 38, "X-SCI": 39, "X-ENG": 11,
                      "XII-PHY": 33, "XII-CHEM": 33, "XII-MATH": 38}


def test_totals_are_80_for_class_10_and_maths_and_70_for_science_12():
    assert {k: get_blueprint(k)["total"] for k in BLUEPRINTS} == {
        "X-MATH-STD": 80, "X-MATH-BASIC": 80, "X-SCI": 80, "X-ENG": 80, "XII-PHY": 70, "XII-CHEM": 70, "XII-MATH": 80}


@pytest.mark.parametrize("key", list(BLUEPRINTS))
def test_a_paper_that_follows_the_blueprint_passes(key):
    bp = get_blueprint(key)
    text, blueprint = validate_paper(_paper(bp), bp["sections"], bp["total"], [])
    assert blueprint["required_marks"] == bp["total"]
    assert text.count("\nQ") + text.startswith("Q") >= pattern_count(bp["sections"][0]["pattern"])


def test_a_missing_question_is_rejected():
    bp = get_blueprint("XII-PHY")

    def drop_last(sections):
        sections[0]["questions"].pop()
        sections[0]["attempt_count"] -= 1

    with pytest.raises(Exception) as err:
        validate_paper(_paper(bp, drop_last), bp["sections"], bp["total"], [])
    assert "count does not match" in str(err.value)


def test_wrong_marks_with_the_right_count_are_rejected():
    bp = get_blueprint("X-MATH-STD")

    def swap(sections):
        sections[1]["questions"][0]["marks"] = 3   # a 2-mark slot written as 3 marks

    with pytest.raises(Exception) as err:
        validate_paper(_paper(bp, swap), bp["sections"], bp["total"], [])
    assert "marks do not match" in str(err.value)


def test_board_papers_cannot_use_optional_choices():
    bp = get_blueprint("XII-PHY")

    def choose(sections):
        sections[1]["attempt_count"] = 4

    with pytest.raises(Exception) as err:
        validate_paper(_paper(bp, choose), bp["sections"], bp["total"], [], allow_choices=True)
    assert "attempt every question" in str(err.value)


def test_college_sections_still_use_percentages():
    sections = [{"label": "Section A", "percent": 40, "bloomLevels": []}, {"label": "Section B", "percent": 60, "bloomLevels": []}]
    assert allocate_sections(sections, 50) == [20, 30]


def test_unknown_or_bad_keys_give_no_blueprint():
    assert get_blueprint("nope") is None and get_blueprint(None) is None and get_blueprint(10) is None


def test_the_returned_blueprint_is_a_copy():
    first = get_blueprint("X-SCI")
    first["sections"][0]["pattern"].append([9, 9])
    assert get_blueprint("X-SCI")["sections"][0]["pattern"] == [[1, 9], [2, 3], [3, 2], [4, 1], [5, 1]]


def test_every_class_from_1_to_8_has_a_starting_layout_that_adds_to_100():
    for class_level in range(1, 9):
        stage = stage_for_class(class_level)
        assert stage and sum(s["percent"] for s in STAGE_DEFAULTS[stage]["sections"]) == 100
    assert stage_for_class(9) is None


def test_catalog_lists_boards_and_stages():
    data = catalog()
    assert [b["key"] for b in data["boards"]] == list(BLUEPRINTS)
    assert all(b["competencyPercent"] == 50 for b in data["boards"])
    assert set(data["stages"]) == {"foundational", "preparatory", "middle"}


def test_internal_choice_counts_follow_each_official_paper():
    """Checked against the 2026-27 sample papers: the choice counts differ between subjects."""
    kinds = lambda key: [s["kind"] for s in get_blueprint(key)["sections"]]
    assert "3 of them" in kinds("XII-MATH")[2]          # Class 12 Maths: 3 choices in Section C
    assert "2 of them" in kinds("X-MATH-STD")[2]        # Class 10 Maths: 2 choices in Section C
    assert "1 of them" in kinds("XII-CHEM")[1]          # Chemistry: 1 choice in Section B
    assert "2 of them" in kinds("XII-PHY")[1]           # Physics: 2 choices in Section B
    assert "answers any 10" in kinds("X-ENG")[1] and "answer any 4" in kinds("X-ENG")[2]
