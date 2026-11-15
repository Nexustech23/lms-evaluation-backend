"""Library figures: every figure draws, parts can be named or lettered, and the letter key is exact."""
import json

import pytest

from app.services.diagram_render import draw_diagram
from app.services.library_figures import (LIBRARY_TYPE, _FIGURES, catalog_text, check_figure, draw_figure, figure_keys,
                                          lettered_parts, part_names)
from app.services.paper_blueprint import validate_paper

PNG = b"\x89PNG\r\n\x1a\n"
KEYS = figure_keys()


def fig(key, **extra):
    return {"type": LIBRARY_TYPE, "title": "t", "figure": key, **extra}


def test_the_library_has_the_planned_figures():
    assert set(KEYS) == {"plant_cell", "animal_cell", "human_heart", "human_eye", "flower_section", "digestive_system",
                         "neuron", "leaf_section", "simple_distillation", "electrolysis_of_water"}


@pytest.mark.parametrize("key", KEYS)
def test_every_figure_draws_with_names_and_with_letters(key):
    assert draw_figure(fig(key)).startswith(PNG)
    assert draw_figure(fig(key, labels="letters")).startswith(PNG)
    assert draw_diagram(fig(key, hide=part_names(key)[:2])).startswith(PNG)


@pytest.mark.parametrize("key", KEYS)
def test_part_lists_are_sound(key):
    entry = _FIGURES[key]
    names = [p[0] for p in entry["parts"]]
    assert len(names) == len(set(names)) and 5 <= len(names) <= 16          # letters A to P are enough
    for name, (x, y), side, label_y in entry["parts"]:
        assert side in ("left", "right")
        assert -1 <= x <= 12 and -1 <= y <= 10.5, (key, name, x, y)       # the anchor sits on the drawing
        assert -1 <= label_y <= 10, (key, name)
    assert len({(p[2], round(p[3], 1)) for p in entry["parts"]}) == len(names)  # no two labels share a spot


def test_letters_follow_the_figures_own_part_order():
    names = part_names("plant_cell")
    assert lettered_parts(fig("plant_cell", labels="letters")) == list(zip("ABCDEFGH", names))
    # hide: only the hidden parts get letters, in the figure's order and not in the order they were listed
    assert lettered_parts(fig("plant_cell", hide=["Vacuole", "Cell wall"])) == [("A", "Cell wall"), ("B", "Vacuole")]
    assert lettered_parts(fig("plant_cell")) == []


def test_the_catalog_names_every_figure_and_part_in_order():
    text = catalog_text()
    for key in KEYS:
        assert f'"{key}"' in text
        assert ", ".join(f"{i + 1}. {n}" for i, n in enumerate(part_names(key))) in text


BAD = [
    (fig("lung"), "figure must be one of"),
    (fig("plant_cell", labels="numbers"), "labels must be"),
    (fig("plant_cell", hide="Nucleus"), "hide must be a list"),
    (fig("plant_cell", hide=["Nucleus", "Wing"]), "unknown part name"),
]


@pytest.mark.parametrize("spec,reason", BAD)
def test_bad_descriptions_are_refused_with_a_reason(spec, reason):
    assert reason in (check_figure(spec) or "")
    with pytest.raises(ValueError):
        draw_figure(spec)


def test_the_general_renderer_never_raises_on_a_bad_figure():
    assert draw_diagram(fig("lung")).startswith(PNG)


SECTIONS = [{"label": "Section A", "percent": 100, "bloomLevels": []}]


def _paper(figure, **question):
    q = {"number": 1, "text": "Name the parts A, B and C.", "marks": 3, "bloom_level": 2, "co_codes": [], "diagrams": [figure]}
    q.update(question)
    return json.dumps({"sections": [{"label": "Section A", "attempt_count": 1, "questions": [q]}]})


def test_a_paper_keeps_a_library_figure():
    text, blueprint = validate_paper(_paper(fig("plant_cell")), SECTIONS, 3, [])
    assert '"library_figure"' in text and blueprint["answer_key"] == []


def test_a_lettered_figure_adds_an_exact_answer_key():
    text, blueprint = validate_paper(_paper(fig("plant_cell", hide=["Nucleus", "Vacuole", "Cell wall"])), SECTIONS, 3, [])
    (entry,) = blueprint["answer_key"]
    assert entry["number"] == 1
    assert entry["working"] == "Figure key: A = Cell wall, B = Vacuole, C = Nucleus"


def test_the_figure_key_joins_the_answer_the_ai_wrote():
    text, blueprint = validate_paper(
        _paper(fig("plant_cell", labels="letters"), answer="A cell wall, B membrane", working="from the diagram"), SECTIONS, 3, [])
    (entry,) = blueprint["answer_key"]
    assert entry["answer"] == "A cell wall, B membrane"
    assert entry["working"].startswith("from the diagram Figure key: A = Cell wall, B = Cell membrane")


def test_a_bad_library_figure_becomes_a_placeholder_and_gives_no_key():
    text, blueprint = validate_paper(_paper(fig("plant_cell", hide=["Wing"])), SECTIONS, 3, [])
    spec = json.loads(text.split("<<<DIAGRAM>>>")[1].split("<<<END_DIAGRAM>>>")[0])
    assert spec["type"] == "placeholder" and "unknown part name" in spec["notes"][0]
    assert blueprint["answer_key"] == [] and blueprint["required_marks"] == 3


@pytest.mark.parametrize("key", ["human_heart", "simple_distillation"])
def test_a_figure_lands_in_the_word_file(key):
    from docx import Document
    from app.services.diagram_render import embed_diagram

    doc = Document()
    embed_diagram(doc, fig(key))
    assert len(doc.inline_shapes) == 1
