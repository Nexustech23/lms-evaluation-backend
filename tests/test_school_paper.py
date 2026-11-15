"""The school-only paper module and the institute type on /me. No AI calls and no database."""
import asyncio
import json

import pytest
from bson import ObjectId

from app.services.cbse_blueprints import get_blueprint
from app.services.school_paper import SchoolPaperError, apply_cbse_preset, board_instructions


def apply(preset="XII-PHY", layout=None, **over):
    args = dict(institute_type="school", total_marks=100, duration="3 Hours", semester="", subject_name="Subject")
    args.update(over)
    return apply_cbse_preset(preset, layout, **args)


def physics_layout(**changes):
    layout = [{"pattern": [list(g) for g in s["pattern"]], "choice": s["choice"]} for s in get_blueprint("XII-PHY")["sections"]]
    for index, pattern in changes.items():
        layout[int(index)]["pattern"] = pattern
    return json.dumps(layout)


# ---- the preset decides the paper settings

def test_an_official_preset_overrides_the_form_fields():
    paper = apply()
    assert (paper.total_marks, paper.duration, paper.semester, paper.subject_name) == (70, "3 Hours", "Class 12", "Physics")
    assert paper.board["customised"] is False and paper.board["key"] == "XII-PHY"


def test_what_the_teacher_typed_is_kept():
    paper = apply(semester="Class 12-A", subject_name="Physics Theory", duration="2 Hours", layout=physics_layout(), total_marks=70)
    assert (paper.semester, paper.subject_name, paper.duration) == ("Class 12-A", "Physics Theory", "2 Hours")
    assert paper.board["customised"] is False                      # the same numbers as the blueprint


def test_a_blank_duration_falls_back_to_the_blueprint():
    assert apply(duration="   ").duration == "3 Hours"
    assert apply(duration="x" * 80).duration == "x" * 30


# ---- an edited layout

def test_an_edited_layout_replaces_the_sections_and_the_total():
    layout = physics_layout(**{"0": [[1, 6]], "1": [[2, 4]], "2": [[3, 4]], "3": [[4, 1]], "4": [[5, 0]]})
    paper = apply(layout=layout, total_marks=30, duration="1.5 Hours")
    assert paper.total_marks == 30 and paper.duration == "1.5 Hours"
    assert paper.board["customised"] is True and len(paper.board["sections"]) == 4


def test_a_layout_that_does_not_add_up_is_refused_with_the_gap():
    with pytest.raises(SchoolPaperError) as err:
        apply(layout=physics_layout(**{"0": [[1, 12]]}), total_marks=70)
    assert err.value.status_code == 400 and "CBSE layout: The sections add up to 66 marks but the total is 70" in err.value.message


@pytest.mark.parametrize("bad", ["not json", "{", "null", '"x"', "[]"])
def test_an_unreadable_layout_is_refused_not_crashed_on(bad):
    with pytest.raises(SchoolPaperError) as err:
        apply(layout=bad, total_marks=70)
    assert err.value.status_code == 400 and err.value.message.startswith("CBSE layout:")


# ---- who may use it

def test_an_unknown_blueprint_is_refused_first():
    for kind in ("school", "college"):
        with pytest.raises(SchoolPaperError, match="Unknown CBSE blueprint"):
            apply(preset="NOPE", institute_type=kind)


@pytest.mark.parametrize("kind", ["college", "coaching", "", None])
def test_only_schools_may_use_a_cbse_blueprint(kind):
    with pytest.raises(SchoolPaperError, match="only for school institutes") as err:
        apply(institute_type=kind)
    assert err.value.status_code == 400


# ---- the wording given to the AI

def test_the_instruction_names_the_class_subject_and_code():
    text = board_instructions(get_blueprint("XII-PHY"))
    assert "CBSE Class 12 Physics (subject code 042)" in text and "roughly 50%" in text
    assert "changed the section layout" not in text


def test_an_edited_paper_tells_the_ai_to_follow_the_edited_rules():
    board = {**get_blueprint("XII-PHY"), "customised": True}
    assert "changed the section layout" in board_instructions(board)


# ---- /me tells the app what kind of institute it is in

class Collection:
    def __init__(self, doc):
        self.doc = doc

    async def find_one(self, *args, **kwargs):
        return self.doc


class FakeDb:
    def __init__(self, **docs):
        self.docs = docs

    def __getitem__(self, name):
        return Collection(self.docs.get(name))


def me(role, **docs):
    from app.api.routers.auth import get_me
    user = {"_id": ObjectId(), "email": "a@b.c", "fullName": "A", "role": role}
    result = asyncio.run(get_me(identity={"role": role, "user_id": str(ObjectId())}, user=user, db=FakeDb(**docs)))
    return result["user"]


@pytest.mark.parametrize("stored,expected", [("school", "school"), ("college", "college"), (None, "college")])
def test_me_gives_the_type_to_an_institute_admin(stored, expected):
    institute = {"_id": ObjectId(), **({"institute_type": stored} if stored else {})}
    assert me(2, instituteDetails=institute)["institute_type"] == expected


def test_me_gives_the_type_to_faculty_and_students():
    institute = {"_id": ObjectId(), "institute_type": "school"}
    faculty = {"_id": ObjectId(), "institute_id": str(ObjectId())}
    assert me(3, facultyDetails=faculty, instituteDetails=institute)["institute_type"] == "school"
    student = {"_id": ObjectId(), "institute_id": str(ObjectId())}
    assert me(4, studentDetails=student, instituteDetails=institute)["institute_type"] == "school"
    assert me(4, studentDetails={"_id": ObjectId(), "institute_id": ObjectId()}, instituteDetails=None)["institute_type"] == "college"


def test_me_leaves_other_roles_alone():
    assert "institute_type" not in me(5, tutorDetails={"_id": ObjectId()})
