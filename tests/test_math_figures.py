"""Maths figures: valid descriptions draw, bad ones are refused cleanly, and one bad figure never sinks a paper."""
import copy
import json

import pytest

from app.services.diagram_render import draw_diagram
from app.services.math_figures import FIGURE_TYPES, check_figure, draw_figure
from app.services.paper_blueprint import validate_paper

PNG = b"\x89PNG\r\n\x1a\n"

GOOD = {
    "geometry_figure": {"type": "geometry_figure", "title": "Triangle ABC", "points": {"A": [0, 0], "B": [6, 0], "C": [0, 8]},
                        "segments": [["A", "B"], ["B", "C"], {"from": "A", "to": "C", "dashed": True}],
                        "labels": [{"at": ["A", "B"], "text": "6 cm"}, {"at": "C", "text": "top"}],
                        "angles": [{"vertex": "A", "from": "B", "to": "C", "right": True},
                                   {"vertex": "B", "from": "A", "to": "C", "text": "53°"}]},
    "circle": {"type": "geometry_figure", "title": "Tangent", "points": {"O": [0, 0], "P": [5, 0], "T": [3, 4]},
               "segments": [["O", "T"], ["T", "P"]], "circles": [{"center": "O", "radius": 3}]},
    "bar_chart": {"type": "bar_chart", "title": "Sales", "categories": ["Mon", "Tue", "Wed"],
                  "series": [{"label": "Shop A", "values": [5, 8, 6]}, {"label": "Shop B", "values": [4, 9, 7]}], "ylabel": "Units"},
    "pie_chart": {"type": "pie_chart", "title": "Budget", "labels": ["Food", "Rent", "Other"], "values": [40, 35, 25], "show_values": False},
    "histogram": {"type": "histogram", "title": "Marks", "edges": [0, 10, 20, 30, 40], "frequencies": [5, 12, 9, 4]},
    "ogive": {"type": "ogive", "title": "Cumulative",
              "series": [{"label": "Less than", "x": [10, 20, 30], "y": [5, 13, 16]}, {"label": "More than", "x": [0, 10, 20], "y": [16, 11, 3]}]},
    "number_line": {"type": "number_line", "title": "Line", "min": -3, "max": 5, "step": 1,
                    "points": [{"value": 2, "label": "A"}, {"value": -1, "label": "B", "open": True}], "intervals": [{"from": -1, "to": 2}]},
    "coordinate_plane": {"type": "coordinate_plane", "title": "Plane", "xrange": [-2, 8], "yrange": [-2, 8],
                         "points": [{"name": "A", "x": 1, "y": 1}, {"name": "B", "x": 6, "y": 1}, {"name": "C", "x": 3, "y": 6}],
                         "segments": [["A", "B"]], "polygons": [["A", "B", "C"]], "lines": [{"through": [[0, 1], [2, 5]], "label": "l"}]},
}


@pytest.mark.parametrize("name", list(GOOD))
def test_valid_figures_draw_a_png(name):
    spec = GOOD[name]
    assert check_figure(spec) is None
    png = draw_figure(spec)
    assert png.startswith(PNG) and len(png) > 3000


def test_every_figure_type_is_covered():
    assert {GOOD[k]["type"] for k in GOOD} == set(FIGURE_TYPES)


def test_the_general_renderer_uses_the_new_figures():
    assert draw_diagram(GOOD["histogram"]).startswith(PNG)


BAD = [
    ("geometry_figure", lambda s: s["points"].update({"Z": [float("nan"), 1]}), "needs a short name"),
    ("geometry_figure", lambda s: s["segments"].append(["A", "Q"]), "must join two different listed points"),
    ("geometry_figure", lambda s: s["angles"].append({"vertex": "A", "from": "A", "to": "B"}), "three different"),
    ("circle", lambda s: s["circles"].__setitem__(0, {"center": "O", "radius": -1}), "positive radius"),
    ("bar_chart", lambda s: s["series"][0]["values"].pop(), "one number per category"),
    ("pie_chart", lambda s: s["values"].__setitem__(0, 0), "positive numbers"),
    ("histogram", lambda s: s["edges"].__setitem__(2, 5), "increasing"),
    ("histogram", lambda s: s["frequencies"].append(3), "one non-negative number per class"),
    ("ogive", lambda s: s["series"][0]["x"].__setitem__(1, 1), "must not decrease"),
    ("number_line", lambda s: s.update({"min": 5, "max": 5}), "max above min"),
    ("number_line", lambda s: s["points"][0].update({"value": 99}), "inside min and max"),
    ("coordinate_plane", lambda s: s["polygons"].append(["A", "B"]), "at least 3"),
    ("coordinate_plane", lambda s: s["lines"][0].update({"through": [[1, 1], [1, 1]]}), "two different"),
]


@pytest.mark.parametrize("name,damage,reason", BAD)
def test_bad_descriptions_are_refused_with_a_reason(name, damage, reason):
    spec = copy.deepcopy(GOOD[name])
    damage(spec)
    assert reason in (check_figure(spec) or "")
    with pytest.raises(ValueError):
        draw_figure(spec)


def test_the_general_renderer_never_raises_on_a_bad_figure():
    spec = copy.deepcopy(GOOD["bar_chart"])
    spec["series"][0]["values"] = []
    assert draw_diagram(spec).startswith(PNG)   # falls back to an error picture


def _paper(figure):
    return json.dumps({"sections": [{"label": "Section A", "attempt_count": 1, "questions": [
        {"number": 1, "text": "Find the area of the triangle shown.", "marks": 4, "bloom_level": 3, "co_codes": [], "diagrams": [figure]}]}]})


SECTIONS = [{"label": "Section A", "percent": 100, "bloomLevels": []}]


def test_a_paper_keeps_a_good_figure():
    text, blueprint = validate_paper(_paper(GOOD["geometry_figure"]), SECTIONS, 4, [])
    assert "<<<DIAGRAM>>>" in text and '"geometry_figure"' in text


def test_a_bad_figure_becomes_a_placeholder_and_the_paper_survives():
    bad = copy.deepcopy(GOOD["bar_chart"])
    bad["series"][0]["values"] = [1]
    text, blueprint = validate_paper(_paper(bad), SECTIONS, 4, [])
    assert blueprint["required_marks"] == 4
    block = text.split("<<<DIAGRAM>>>")[1].split("<<<END_DIAGRAM>>>")[0]
    spec = json.loads(block)
    assert spec["type"] == "placeholder" and "could not be drawn automatically" in spec["notes"][0]
    assert "one number per category" in spec["notes"][0]


def test_old_diagram_types_still_work_in_papers():
    table = {"type": "data_table", "title": "Marks", "headers": ["a", "b"], "rows": [[1, 2]]}
    text, _ = validate_paper(_paper(table), SECTIONS, 4, [])
    assert '"data_table"' in text


@pytest.mark.parametrize("name", ["geometry_figure", "histogram", "number_line"])
def test_a_figure_lands_in_the_word_file_as_a_picture_with_a_caption(name):
    from docx import Document
    from app.services.diagram_render import embed_diagram

    doc = Document()
    embed_diagram(doc, GOOD[name])
    assert len(doc.inline_shapes) == 1
    assert any(p.text.startswith("Figure:") for p in doc.paragraphs)
