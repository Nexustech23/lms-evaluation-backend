"""A teacher can edit a CBSE blueprint: the edited layout is checked, turned into sections, and enforced. No AI calls."""
import json

import pytest

from app.services.cbse_blueprints import (
    BLUEPRINTS, LayoutError, catalog, custom_sections, get_blueprint, is_customised, pattern_count, pattern_marks,
)
from app.services.paper_blueprint import validate_paper


def layout_of(key):
    """The official layout, as the form would send it back untouched."""
    return [{"pattern": [list(g) for g in s["pattern"]], "choice": s["choice"]} for s in get_blueprint(key)["sections"]]


def paper_for(sections):
    n, out = 0, []
    for sec in sections:
        questions = []
        for marks, count in sec["pattern"]:
            for _ in range(count):
                n += 1
                questions.append({"number": n, "text": f"Question {n} about topic {n}", "marks": marks,
                                  "bloom_level": 2, "co_codes": [], "diagrams": []})
        out.append({"label": sec["label"], "attempt_count": len(questions), "questions": questions})
    return json.dumps({"sections": out})


# ---- the form gets what it needs to show and edit the card

def test_the_catalog_carries_the_pattern_and_choice_for_every_section():
    for board in catalog()["boards"]:
        for sec in board["sections"]:
            assert sec["pattern"] and isinstance(sec["choice"], (int, type(None))) and sec["style"]
            assert sum(m * c for m, c in sec["pattern"]) == sec["marks"]


# ---- an untouched layout is the official blueprint

@pytest.mark.parametrize("key", list(BLUEPRINTS))
def test_an_untouched_layout_gives_the_official_sections(key):
    official = get_blueprint(key)
    got = custom_sections(key, layout_of(key), official["total"])
    assert [(s["label"], s["pattern"], s["kind"]) for s in got] == \
           [(s["label"], s["pattern"], s["kind"]) for s in official["sections"]]
    assert not is_customised(key, layout_of(key), official["total"])


# ---- the small mid-semester paper from the 70-mark Physics blueprint

def physics_mid_sem():
    layout = layout_of("XII-PHY")
    layout[0]["pattern"] = [[1, 6]]          # 6 MCQ                      6
    layout[1]["pattern"] = [[2, 4]]          # 4 very short answer        8
    layout[2]["pattern"] = [[3, 4]]          # 4 short answer            12
    layout[3]["pattern"] = [[4, 1]]          # 1 case-based question      4
    layout[4]["pattern"] = [[5, 0]]          # long answer left out       0   -> 30 marks
    return layout


def test_a_70_mark_paper_becomes_a_30_mark_mid_sem():
    sections = custom_sections("XII-PHY", physics_mid_sem(), 30)
    assert [s["label"] for s in sections] == ["Section A", "Section B", "Section C", "Section D"]
    assert sum(pattern_marks(s["pattern"]) for s in sections) == 30
    assert [s["percent"] for s in sections] == [20.0, 26.67, 40.0, 13.33]
    assert is_customised("XII-PHY", physics_mid_sem(), 30)


def test_the_edited_paper_is_enforced_by_the_validator():
    sections = custom_sections("XII-PHY", physics_mid_sem(), 30)
    _, blueprint = validate_paper(paper_for(sections), sections, 30, [])
    assert blueprint["required_marks"] == 30
    short = json.loads(paper_for(sections))
    short["sections"][0]["questions"].pop()
    short["sections"][0]["attempt_count"] -= 1
    with pytest.raises(Exception) as err:
        validate_paper(json.dumps(short), sections, 30, [])
    assert "count does not match" in str(err.value)


def test_an_edited_section_no_longer_carries_the_old_counts():
    sections = custom_sections("XII-PHY", physics_mid_sem(), 30)
    kinds = " ".join(s["kind"] for s in sections)
    for stale in ("16 multiple-choice", "5 very short", "7 short answer", "3 long answer"):
        assert stale not in kinds
    assert "multiple-choice questions" in sections[0]["kind"] and "no internal choice" in sections[0]["kind"]


def test_a_section_left_as_it_was_keeps_the_official_text():
    layout = layout_of("XII-PHY")
    layout[0]["pattern"] = [[1, 10]]
    layout[1]["pattern"] = [[2, 5]]
    layout[2]["pattern"] = [[3, 7]]
    layout[3]["pattern"] = [[4, 2]]
    layout[4]["pattern"] = [[5, 3]]
    sections = custom_sections("XII-PHY", layout, 64)
    official = get_blueprint("XII-PHY")["sections"]
    assert sections[0]["kind"] != official[0]["kind"]
    assert [s["kind"] for s in sections[1:]] == [s["kind"] for s in official[1:]]


# ---- the numbers must make a paper

def test_marks_that_do_not_add_up_are_rejected_with_the_gap():
    layout = layout_of("XII-PHY")
    layout[0]["pattern"] = [[1, 12]]         # 4 marks fewer than the 70 total
    with pytest.raises(LayoutError, match="add up to 66 marks but the total is 70"):
        custom_sections("XII-PHY", layout, 70)


@pytest.mark.parametrize("group,message", [
    ([0, 5], "marks per question must be between 1 and 20"),
    ([21, 5], "marks per question must be between 1 and 20"),
    ([1.5, 5], "marks per question must be a whole number"),
    (["2", 5], "marks per question must be a whole number"),
    ([True, 5], "marks per question must be a whole number"),
    ([2, -1], "number of questions must be between 0 and 100"),
    ([2, 2.5], "number of questions must be a whole number"),
    ([2, 101], "number of questions must be between 0 and 100"),
    ([2], "each group needs marks and a number of questions"),
])
def test_impossible_numbers_are_rejected(group, message):
    layout = layout_of("XII-PHY")
    layout[1]["pattern"] = [group]
    with pytest.raises(LayoutError, match=message):
        custom_sections("XII-PHY", layout, 70)


def test_the_total_itself_is_checked():
    for bad in (0, -5, 501, 70.5, "70", None):
        with pytest.raises(LayoutError, match="Total marks"):
            custom_sections("XII-PHY", layout_of("XII-PHY"), bad)


def test_a_layout_that_does_not_fit_the_blueprint_is_rejected():
    layout = layout_of("XII-PHY")
    with pytest.raises(LayoutError, match="does not match the blueprint's sections"):
        custom_sections("XII-PHY", layout[:-1], 70)
    with pytest.raises(LayoutError, match="does not match the blueprint's sections"):
        custom_sections("XII-PHY", "nonsense", 70)
    layout[0]["pattern"] = [[1, 8], [1, 8]]
    with pytest.raises(LayoutError, match="Section A: the layout does not match"):
        custom_sections("XII-PHY", layout, 70)
    with pytest.raises(LayoutError, match="Unknown CBSE blueprint"):
        custom_sections("NOPE", [], 70)


def test_a_paper_with_every_section_emptied_is_rejected():
    layout = layout_of("XII-PHY")
    for sec in layout:
        sec["pattern"] = [[sec["pattern"][0][0], 0]]
    with pytest.raises(LayoutError, match="At least one section"):
        custom_sections("XII-PHY", layout, 70)


# ---- internal choice

def test_internal_choice_cannot_exceed_the_questions_and_follows_the_edit():
    layout = layout_of("XII-PHY")
    layout[4]["pattern"] = [[5, 2]]          # was 3 long-answer questions, all with choice
    layout[4]["choice"] = 3
    sections = custom_sections("XII-PHY", layout, 65)
    assert "in all 2" in sections[4]["kind"]
    layout = layout_of("XII-PHY")
    layout[1]["choice"] = 4                  # of 5 very short answers
    sections = custom_sections("XII-PHY", layout, 70)
    assert "in 4 of them" in sections[1]["kind"] and is_customised("XII-PHY", layout, 70)
    layout[1]["choice"] = 0
    assert "no internal choice" in custom_sections("XII-PHY", layout, 70)[1]["kind"]


def test_a_bad_choice_value_is_rejected():
    layout = layout_of("XII-PHY")
    layout[1]["choice"] = -1
    with pytest.raises(LayoutError, match="internal choice"):
        custom_sections("XII-PHY", layout, 70)


def test_case_based_sections_keep_their_choice_rule_and_flag():
    sections = custom_sections("XII-PHY", physics_mid_sem(), 30)
    assert sections[3].get("caseBased") is True and "internal choice follows the CBSE format" in sections[3]["kind"]


# ---- sections with more than one kind of question

def test_a_multi_group_section_can_drop_one_group():
    layout = layout_of("X-SCI")
    layout[2]["pattern"][4] = [5, 0]         # Physics: no 5-mark question (5 marks fewer)
    sections = custom_sections("X-SCI", layout, 75)
    assert sections[2]["pattern"] == [[1, 3], [2, 2], [3, 3], [4, 1]]
    assert sum(pattern_marks(s["pattern"]) for s in sections) == 75
    _, blueprint = validate_paper(paper_for(sections), sections, 75, [])
    assert blueprint["required_marks"] == 75


def test_english_and_science_sections_can_be_edited_too():
    for key in ("X-SCI", "X-ENG"):
        layout = layout_of(key)
        official = get_blueprint(key)
        layout[0]["pattern"][0][1] += 1
        extra = layout[0]["pattern"][0][0]
        sections = custom_sections(key, layout, official["total"] + extra)
        assert pattern_count(sections[0]["pattern"]) == pattern_count(official["sections"][0]["pattern"]) + 1
        assert sections[0]["kind"] != official["sections"][0]["kind"]


# ---- what counts as customised

def test_customised_means_any_difference_from_the_official_blueprint():
    key = "XII-PHY"
    assert not is_customised(key, layout_of(key), 70)
    assert is_customised(key, layout_of(key), 60)
    layout = layout_of(key)
    layout[2]["pattern"] = [[3, 8]]
    assert is_customised(key, layout, 73)
    layout = layout_of(key)
    layout[3]["choice"] = 1                  # case-based: the choice number is not editable, so no change
    assert not is_customised(key, layout, 70)
    assert is_customised(key, "bad", 70) and is_customised("NOPE", [], 70)


def test_the_official_blueprint_is_never_changed_by_an_edit():
    before = json.dumps(get_blueprint("XII-PHY"), sort_keys=True)
    custom_sections("XII-PHY", physics_mid_sem(), 30)
    assert json.dumps(get_blueprint("XII-PHY"), sort_keys=True) == before


# ---- the prompt the AI is given

def test_the_prompt_for_an_edited_paper_states_the_edited_counts():
    from app.api.routers.question_paper import _build_claude_prompt
    sections = custom_sections("XII-PHY", physics_mid_sem(), 30)
    board = {"key": "XII-PHY", "code": "042", "class": 12, "subject": "Physics", "customised": True}
    text = _build_claude_prompt("Ray optics and current", "", "", "School", "Physics", "Mid Semester Examination",
                                "Class 12", "2026-27", 30, "1.5 Hours", sections, [], board=board)
    assert "Section A: 6 marks — EXACTLY 6 questions: 6 x 1 mark" in text
    assert "Section D: 4 marks — EXACTLY 1 questions: 1 x 4 marks" in text
    assert "Section E: " not in text
    assert "changed the section layout" in text
    official = {**board, "customised": False}
    plain = _build_claude_prompt("Ray optics", "", "", "School", "Physics", "Annual", "Class 12", "2026-27", 70, "3 Hours",
                                 get_blueprint("XII-PHY")["sections"], [], board=official)
    assert "changed the section layout" not in plain and "Section E: 15 marks — EXACTLY 3 questions: 3 x 5 marks" in plain
