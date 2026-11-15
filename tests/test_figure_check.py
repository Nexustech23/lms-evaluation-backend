"""Figures against their questions: each check raises a flag when text and figure disagree, and stays quiet when they agree."""
import json

import pytest

from app.services.figure_check import check_figures, merge_flags
from app.services.paper_blueprint import validate_paper


def flags(text, *diagrams, number=1):
    return check_figures([{"questions": [{"number": number, "text": text, "diagrams": list(diagrams)}]}])


def issue(text, *diagrams):
    result = flags(text, *diagrams)
    return result[0]["issue"] if result else ""


TRIANGLE = {"type": "geometry_figure", "title": "Triangle ABC", "points": {"A": [0, 0], "B": [6, 0], "C": [0, 8]},
            "segments": [["A", "B"], ["B", "C"], ["A", "C"]],
            "labels": [{"at": ["A", "B"], "text": "6 cm"}, {"at": ["A", "C"], "text": "8 cm"}],
            "angles": [{"vertex": "A", "from": "B", "to": "C", "right": True}, {"vertex": "B", "from": "A", "to": "C", "text": "53°"}]}


def tri(**changes):
    spec = json.loads(json.dumps(TRIANGLE))
    spec.update(changes)
    return spec


# ---- a question that points at a figure that is not there

@pytest.mark.parametrize("text", ["In the given figure, find x.", "Using the diagram shown below, find the area.",
                                  "Study the following graph and answer.", "The figure above shows a circuit."])
def test_a_reference_to_a_missing_figure_is_flagged(text):
    assert "none is attached" in issue(text)


def test_no_flag_when_the_figure_is_there_or_not_mentioned():
    assert flags("In the given figure, find x.", TRIANGLE) == []
    assert flags("Find the area of a triangle with sides 3, 4 and 5.") == []
    assert flags("Draw the graph of y = x^2.") == []          # asks the student to draw one, does not point at one


# ---- a figure that could not be drawn

def test_a_figure_that_cannot_be_drawn_is_flagged_with_the_reason():
    bad = {"type": "bar_chart", "title": "t", "categories": ["a", "b"], "series": [{"label": "s", "values": [1]}]}
    text = issue("Study the chart.", bad)
    assert "could not be drawn automatically" in text and "one number per category" in text


# ---- geometry

def test_a_consistent_triangle_is_quiet():
    assert flags("In triangle ABC, AB = 6 cm and AC = 8 cm. Find BC.", TRIANGLE) == []


def test_an_angle_label_that_does_not_match_the_drawing():
    wrong = tri(angles=[{"vertex": "B", "from": "A", "to": "C", "text": "60°"}])
    assert "labelled 60° but is drawn as 53°" in issue("Find x.", wrong)


def test_a_right_angle_mark_on_an_angle_that_is_not_right():
    wrong = tri(angles=[{"vertex": "B", "from": "A", "to": "C", "right": True}])
    assert "marked as a right angle but is drawn as 53°" in issue("Find x.", wrong)


def test_lengths_that_are_not_drawn_to_scale():
    wrong = tri(labels=[{"at": ["A", "B"], "text": "6 cm"}, {"at": ["A", "C"], "text": "5 cm"}])   # AC is drawn longer than AB
    assert "not to scale" in issue("Find BC.", wrong)


def test_the_text_and_the_label_disagree_about_a_length():
    assert "The question says AB = 5 but the figure labels it 6 cm" in issue("In triangle ABC, AB = 5 cm. Find BC.", TRIANGLE)


def test_the_text_and_the_drawing_disagree_about_an_angle():
    assert "∠ABC = 70° but the figure draws it as 53°" in issue("In the figure, ∠ABC = 70°. Find ∠ACB.", TRIANGLE)
    assert flags("In the figure, ∠ABC = 53°. Find ∠ACB.", TRIANGLE) == []


# ---- coordinate plane

PLANE = {"type": "coordinate_plane", "title": "p", "xrange": [-2, 8], "yrange": [-2, 8],
         "points": [{"name": "A", "x": 1, "y": 2}, {"name": "B", "x": 6, "y": 2}], "segments": [["A", "B"]]}


def test_plotted_points_against_the_text():
    assert flags("The points A(1, 2) and B(6, 2) are joined. Find AB.", PLANE) == []
    assert "gives A(2, 3) but the figure plots A at (1, 2)" in issue("The point A(2, 3) is joined to B. Find AB.", PLANE)


def test_a_point_outside_the_drawn_area():
    off = json.loads(json.dumps(PLANE))
    off["points"][1]["x"] = 20
    assert "outside the drawn area" in issue("Plot the points.", off)


# ---- physics

def lens(**extra):
    return {"type": "ray_diagram", "element": "convex_lens", "focal_length": 10, "object_distance": 30, **extra}


def test_the_element_in_the_text_must_match_the_figure():
    assert "mentions a concave mirror but the figure shows a convex lens" in issue("A concave mirror forms an image. Find its position.", lens())
    assert flags("A convex lens of focal length 10 cm forms an image of an object 30 cm in front of it.", lens()) == []


def test_focal_length_and_object_distance_must_match():
    assert "focal length of 15 cm but the figure uses 10 cm" in issue("A convex lens has a focal length of 15 cm.", lens())
    assert "object 25 cm away but the figure uses 30 cm" in issue("An object is placed 25 cm in front of the lens.", lens())


def test_a_figure_that_already_shows_what_the_student_must_draw():
    assert "already shows the rays" in issue("Draw the ray diagram for the image formed.", lens(show_rays=True))
    assert flags("Draw the ray diagram for the image formed.", lens()) == []
    assert flags("Explain how the image in the figure is formed.", lens(show_rays=True)) == []


def slab(**extra):
    return {"type": "refraction", "kind": "slab", "angle_of_incidence": 40, "n": 1.5, **extra}


def test_refraction_values_in_the_text():
    assert "angle of incidence of 30° but the figure uses 40" in issue("The angle of incidence is 30°. Find r.", slab())
    assert "refractive index of 1.6 but the figure uses 1.5" in issue("The refractive index of glass is 1.6.", slab())
    assert flags("A ray falls at an angle of incidence 40° on a glass of refractive index 1.5.", slab()) == []


def test_prism_and_slab_are_not_mixed_up():
    assert "mentions a prism but the figure shows a glass slab" in issue("A ray passes through a prism.", slab())
    prism = {"type": "refraction", "kind": "prism", "angle_of_incidence": 40, "n": 1.5, "prism_angle": 60}
    assert "prism angle of 50° but the figure uses 60" in issue("The angle of the prism is 50°.", prism)


def test_refraction_path_given_away_only_for_drawing_tasks():
    assert "already shows the refracted path" in issue("Draw the path of the ray through the slab.", slab())
    assert flags("Draw the path of the ray through the slab.", slab(show_path=False)) == []
    assert flags("Calculate the angle of refraction.", slab()) == []        # showing the path is fine when the value is asked


def test_magnet_kind_must_match():
    wire = {"type": "magnetic_field", "kind": "straight_wire"}
    assert "mentions a bar magnet" in issue("Draw the field lines of a bar magnet.", wire)
    assert flags("The field around a straight current-carrying wire.", wire) == []


# ---- library figures

PLANT = {"type": "library_figure", "figure": "plant_cell"}


def test_the_figure_must_be_the_one_the_question_is_about():
    assert "about the animal cell but the figure shows the plant cell" in issue("Label the parts of an animal cell.", PLANT)
    assert flags("Label the parts of a plant cell.", PLANT) == []
    assert flags("State two differences between a plant and an animal cell.", PLANT) == []


def test_lettered_parts_against_the_question():
    three = {**PLANT, "hide": ["Cell wall", "Vacuole", "Nucleus"]}
    assert flags("Name the parts A, B and C in the diagram.", three) == []
    assert "refers to part D but the figure only has letters A to C" in issue("Name the parts labelled A to D.", three)
    assert "shows every part by name" in issue("Name the parts A and B.", PLANT)


def test_a_question_that_names_the_hidden_part():
    three = {**PLANT, "hide": ["Cell wall", "Vacuole", "Nucleus"]}
    assert "names the Nucleus, which the figure hides as C" in issue("Name part C, the nucleus, and state its function.", three)


# ---- whole papers

def test_flags_are_per_question_sorted_and_combined():
    sections = [{"questions": [
        {"number": 3, "text": "In the given figure, find x.", "diagrams": []},
        {"number": 1, "text": "A concave mirror. Find the image.", "diagrams": [lens()]},
        {"number": 2, "text": "Find the area of a square of side 4.", "diagrams": []}]}]
    assert [f["number"] for f in check_figures(sections)] == [1, 3]


def test_a_quiet_paper_has_no_flags_and_bad_input_does_not_crash():
    assert check_figures([]) == [] and check_figures(None) == []
    assert check_figures([{"questions": [{"number": 1, "text": None, "diagrams": [None, "x", {}]}]}]) == []


def test_merge_joins_issues_for_the_same_question():
    merged = merge_flags([{"number": 2, "issue": "answer differs"}], [{"number": 1, "issue": "figure"}, {"number": 2, "issue": "scale"}])
    assert merged == [{"number": 1, "issue": "figure"}, {"number": 2, "issue": "answer differs; scale"}]
    assert merge_flags([], None) == []


def test_it_works_on_the_paper_the_validator_returns():
    paper = json.dumps({"sections": [{"label": "Section A", "attempt_count": 1, "questions": [
        {"number": 1, "text": "In triangle ABC, AB = 5 cm. Find BC.", "marks": 3, "bloom_level": 3, "co_codes": [], "diagrams": [TRIANGLE]}]}]})
    _, blueprint = validate_paper(paper, [{"label": "Section A", "percent": 100, "bloomLevels": []}], 3, [])
    (found,) = check_figures(blueprint["sections"])
    assert found["number"] == 1 and "AB = 5" in found["issue"]
