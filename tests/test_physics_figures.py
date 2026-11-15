"""Physics figures: positions follow the lens/mirror formula and Snell's law; bad figures are refused cleanly."""
import copy
import json
import math

import numpy as np
import pytest

from app.services.diagram_render import draw_diagram
from app.services.paper_blueprint import validate_paper
from app.services.physics_figures import (PHYSICS_FIGURE_TYPES, _prism_path, _refract, check_figure, draw_figure,
                                          image_info)

PNG = b"\x89PNG\r\n\x1a\n"


def ray(element, f, d, **extra):
    return {"type": "ray_diagram", "title": "t", "element": element, "focal_length": f, "object_distance": d, **extra}


@pytest.mark.parametrize("element,f,d,v,m,real", [
    ("convex_lens", 10, 30, 15.0, -0.5, True),
    ("convex_lens", 10, 15, 30.0, -2.0, True),
    ("convex_lens", 10, 5, -10.0, 2.0, False),
    ("concave_lens", 10, 20, -20 / 3, 1 / 3, False),
    ("concave_mirror", 10, 30, -15.0, -0.5, True),
    ("concave_mirror", 10, 5, 10.0, 2.0, False),
    ("convex_mirror", 10, 20, 20 / 3, 1 / 3, False),
])
def test_image_follows_the_lens_and_mirror_formula(element, f, d, v, m, real):
    info = image_info(ray(element, f, d))
    assert info["v"] == pytest.approx(v) and info["m"] == pytest.approx(m)
    assert info["real"] is real and info["upright"] is (m > 0)


def test_plane_mirror_image_is_as_far_behind_and_the_same_size():
    info = image_info({"element": "plane_mirror", "object_distance": 12, "object_height": 3})
    assert info["v"] == 12 and info["height"] == 3 and not info["real"]


def test_object_at_the_focus_gives_no_image():
    assert image_info(ray("convex_lens", 10, 10)) is None
    assert image_info(ray("concave_mirror", 10, 10)) is None


def test_snell_law_in_vector_form():
    i = 40.0
    d = np.array([math.sin(math.radians(i)), -math.cos(math.radians(i))])
    out = _refract(d, np.array([0.0, -1.0]), 1.0, 1.5)
    r = math.degrees(math.asin(abs(out[0])))
    assert math.sin(math.radians(i)) == pytest.approx(1.5 * math.sin(math.radians(r)), abs=1e-9)


def test_prism_deviation_matches_the_textbook_formula():
    spec = {"type": "refraction", "kind": "prism", "angle_of_incidence": 40, "n": 1.5, "prism_angle": 60}
    path = _prism_path(spec)
    r1 = math.degrees(math.asin(math.sin(math.radians(40)) / 1.5))
    e = math.degrees(math.asin(1.5 * math.sin(math.radians(60 - r1))))
    expected = 40 + e - 60
    actual = math.degrees(math.acos(float(np.dot(path["d_in"], path["out"]))))
    assert actual == pytest.approx(expected, abs=0.01)


GOOD = [
    ray("convex_lens", 10, 30), ray("convex_lens", 10, 30, show_rays=True, show_image=True),
    ray("convex_lens", 10, 5, show_rays=True, show_image=True), ray("convex_lens", 10, 10, show_rays=True, show_image=True),
    ray("concave_lens", 10, 20, show_rays=True, show_image=True),
    ray("concave_mirror", 10, 30, show_rays=True, show_image=True), ray("concave_mirror", 10, 5, show_rays=True, show_image=True),
    ray("convex_mirror", 10, 20, show_rays=True, show_image=True),
    {"type": "ray_diagram", "element": "plane_mirror", "object_distance": 8, "show_rays": True, "show_image": True},
    {"type": "refraction", "kind": "slab", "angle_of_incidence": 40, "n": 1.5},
    {"type": "refraction", "kind": "slab", "angle_of_incidence": 40, "n": 1.5, "show_path": False},
    {"type": "refraction", "kind": "prism", "angle_of_incidence": 40, "n": 1.5, "prism_angle": 60},
    {"type": "refraction", "kind": "prism", "angle_of_incidence": 40, "n": 1.5, "prism_angle": 60, "show_path": False},
    {"type": "magnetic_field", "kind": "bar_magnet"}, {"type": "magnetic_field", "kind": "bar_magnet", "north": "right"},
    {"type": "magnetic_field", "kind": "solenoid"},
    {"type": "magnetic_field", "kind": "straight_wire"}, {"type": "magnetic_field", "kind": "straight_wire", "direction": "in"},
]


@pytest.mark.parametrize("spec", GOOD, ids=lambda s: f"{s['type']}-{s.get('element') or s.get('kind')}-{len(json.dumps(s))}")
def test_valid_figures_draw_a_png(spec):
    assert check_figure(spec) is None
    assert draw_figure(spec).startswith(PNG)
    assert draw_diagram(spec).startswith(PNG)


def test_every_physics_type_is_covered():
    assert {s["type"] for s in GOOD} == set(PHYSICS_FIGURE_TYPES)


BAD = [
    (ray("thick_lens", 10, 30), "element must be one of"),
    (ray("convex_lens", -5, 30), "focal_length"),
    (ray("convex_lens", 10, 0), "object_distance"),
    (ray("convex_lens", 10, 30, object_height=0), "object_height"),
    ({"type": "refraction", "kind": "slab", "angle_of_incidence": 95, "n": 1.5}, "angle_of_incidence"),
    ({"type": "refraction", "kind": "slab", "angle_of_incidence": 40, "n": 0.9}, "n must be above 1"),
    ({"type": "refraction", "kind": "lens", "angle_of_incidence": 40, "n": 1.5}, "kind must be"),
    ({"type": "refraction", "kind": "prism", "angle_of_incidence": 40, "n": 1.5}, "prism_angle"),
    ({"type": "refraction", "kind": "prism", "angle_of_incidence": 60, "n": 1.5, "prism_angle": 90}, "total internal reflection"),
    ({"type": "magnetic_field", "kind": "electromagnet"}, "kind must be"),
    ({"type": "magnetic_field", "kind": "straight_wire", "direction": "up"}, "direction"),
]


@pytest.mark.parametrize("spec,reason", BAD)
def test_bad_descriptions_are_refused_with_a_reason(spec, reason):
    assert reason in (check_figure(spec) or "")
    with pytest.raises(ValueError):
        draw_figure(spec)


def test_the_general_renderer_never_raises_on_a_bad_figure():
    assert draw_diagram(ray("thick_lens", 10, 30)).startswith(PNG)


SECTIONS = [{"label": "Section A", "percent": 100, "bloomLevels": []}]


def _paper(figure):
    return json.dumps({"sections": [{"label": "Section A", "attempt_count": 1, "questions": [
        {"number": 1, "text": "Study the figure.", "marks": 3, "bloom_level": 3, "co_codes": [], "diagrams": [figure]}]}]})


def test_a_paper_keeps_a_good_physics_figure():
    text, _ = validate_paper(_paper(ray("convex_lens", 10, 30)), SECTIONS, 3, [])
    assert '"ray_diagram"' in text


def test_a_bad_physics_figure_becomes_a_placeholder_and_the_paper_survives():
    bad = {"type": "refraction", "kind": "prism", "angle_of_incidence": 60, "n": 1.5, "prism_angle": 90}
    text, blueprint = validate_paper(_paper(bad), SECTIONS, 3, [])
    assert blueprint["required_marks"] == 3
    spec = json.loads(text.split("<<<DIAGRAM>>>")[1].split("<<<END_DIAGRAM>>>")[0])
    assert spec["type"] == "placeholder" and "total internal reflection" in spec["notes"][0]


@pytest.mark.parametrize("spec", [GOOD[1], GOOD[9], GOOD[13]], ids=["ray", "slab", "magnet"])
def test_a_figure_lands_in_the_word_file(spec):
    from docx import Document
    from app.services.diagram_render import embed_diagram

    doc = Document()
    embed_diagram(doc, spec)
    assert len(doc.inline_shapes) == 1
