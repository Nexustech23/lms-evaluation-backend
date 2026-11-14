"""CBSE paper blueprints for school institutes.

Classes 9-12 use the published 2026-27 sample-paper designs: a fixed section layout per subject code.
Checked on 14 Nov 2026 against the official sample papers at cbseacademic.nic.in/web_material/SQP/
(ClassX_2026_27: MathsStandard, MathsBasic, Science, EnglishL; ClassXII_2026_27: Physics, Chemistry, Maths).
Classes 1-8 have no board pattern, so they get editable starting sections only.
A blueprint becomes the same `sections` list the paper generator already uses, plus a `pattern`
(marks, count) per section that the validator enforces. Nothing here touches college papers.
"""
from copy import deepcopy

COMPETENCY_PERCENT = 50  # CBSE: about half of a Class 9-12 paper is competency-focused

_MATHS_80 = [
    {"label": "Section A", "pattern": [[1, 20]], "kind": "20 multiple-choice questions (18 MCQ and 2 assertion-reason), no internal choice"},
    {"label": "Section B", "pattern": [[2, 5]], "kind": "5 very short answer questions; internal choice in 2 of them"},
    {"label": "Section C", "pattern": [[3, 6]], "kind": "6 short answer questions; internal choice in 2 of them"},
    {"label": "Section D", "pattern": [[5, 4]], "kind": "4 long answer questions; internal choice in 2 of them"},
    {"label": "Section E", "pattern": [[4, 3]], "kind": "3 case-study questions of 4 marks (sub-parts 1+1+2); internal choice in the 2-mark sub-part of each", "caseBased": True},
]

_MATHS_XII_80 = [
    {"label": "Section A", "pattern": [[1, 20]], "kind": "20 multiple-choice questions (18 MCQ and 2 assertion-reason), no internal choice"},
    {"label": "Section B", "pattern": [[2, 5]], "kind": "5 very short answer questions; internal choice in 2 of them"},
    {"label": "Section C", "pattern": [[3, 6]], "kind": "6 short answer questions; internal choice in 3 of them"},
    {"label": "Section D", "pattern": [[5, 4]], "kind": "4 long answer questions; internal choice in 2 of them"},
    {"label": "Section E", "pattern": [[4, 3]], "kind": "3 case-study questions of 4 marks; internal choice in one sub-part of 2 of them", "caseBased": True},
]

_CHEMISTRY_70 = [
    {"label": "Section A", "pattern": [[1, 16]], "kind": "16 multiple-choice questions (12 MCQ and 4 assertion-reason), no internal choice"},
    {"label": "Section B", "pattern": [[2, 5]], "kind": "5 very short answer questions; internal choice in 1 of them"},
    {"label": "Section C", "pattern": [[3, 7]], "kind": "7 short answer questions; internal choice in 1 of them"},
    {"label": "Section D", "pattern": [[4, 2]], "kind": "2 case-based questions of 4 marks; any internal choice sits inside a sub-part, not the whole question", "caseBased": True},
    {"label": "Section E", "pattern": [[5, 3]], "kind": "3 long answer questions with internal choice in all 3"},
]

_SCIENCE_70 = [
    {"label": "Section A", "pattern": [[1, 16]], "kind": "16 multiple-choice questions (12 MCQ and 4 assertion-reason), no internal choice"},
    {"label": "Section B", "pattern": [[2, 5]], "kind": "5 very short answer questions; internal choice in 2 of them"},
    {"label": "Section C", "pattern": [[3, 7]], "kind": "7 short answer questions; internal choice in 1 of them"},
    {"label": "Section D", "pattern": [[4, 2]], "kind": "2 case-study questions of 4 marks", "caseBased": True},
    {"label": "Section E", "pattern": [[5, 3]], "kind": "3 long answer questions with internal choice in all 3"},
]

BLUEPRINTS = {
    "X-MATH-STD": {"code": "041", "class": 10, "subject": "Mathematics (Standard)", "total": 80, "duration": "3 Hours", "sections": _MATHS_80},
    "X-MATH-BASIC": {"code": "241", "class": 10, "subject": "Mathematics (Basic)", "total": 80, "duration": "3 Hours", "sections": _MATHS_80},
    "X-SCI": {
        "code": "086", "class": 10, "subject": "Science", "total": 80, "duration": "3 Hours",
        "sections": [
            {"label": "Section A: Biology", "pattern": [[1, 9], [2, 3], [3, 2], [4, 1], [5, 1]], "kind": "Biology: MCQ, assertion-reason, short and long answers, one 4-mark case-based question", "caseBased": True},
            {"label": "Section B: Chemistry", "pattern": [[1, 8], [2, 1], [3, 2], [4, 1], [5, 1]], "kind": "Chemistry: MCQ, assertion-reason, short and long answers, one 4-mark case-based question", "caseBased": True},
            {"label": "Section C: Physics", "pattern": [[1, 3], [2, 2], [3, 3], [4, 1], [5, 1]], "kind": "Physics: MCQ, numerical, short and long answers, one 4-mark case-based question", "caseBased": True},
        ],
    },
    "X-ENG": {
        "code": "184", "class": 10, "subject": "English Language and Literature", "total": 80, "duration": "3 Hours",
        "sections": [
            {"label": "Section A: Reading", "pattern": [[10, 2]], "kind": "2 unseen passages of 10 marks each, with 1-mark and 2-mark sub-parts (one factual case-based passage)", "caseBased": True},
            {"label": "Section B: Grammar and Creative Writing", "pattern": [[10, 1], [5, 2]], "kind": "one grammar question of 12 one-mark tasks of which the student answers any 10, then two 5-mark writing questions each with an A or B alternative"},
            {"label": "Section C: Literature", "pattern": [[5, 2], [12, 1], [6, 3]], "kind": "2 extract questions of 5 marks (A or B), one question of 5 short answers of 3 marks (answer any 4), one question of 3 short answers of 3 marks (answer any 2), 2 long questions of 6 marks each with an alternative"},
        ],
    },
    "XII-PHY": {"code": "042", "class": 12, "subject": "Physics", "total": 70, "duration": "3 Hours", "sections": _SCIENCE_70},
    "XII-CHEM": {"code": "043", "class": 12, "subject": "Chemistry", "total": 70, "duration": "3 Hours", "sections": _CHEMISTRY_70},
    "XII-MATH": {"code": "041", "class": 12, "subject": "Mathematics", "total": 80, "duration": "3 Hours", "sections": _MATHS_XII_80},
}

# Classes 1-8: CBSE sets no marks total or paper pattern, so these are starting points a teacher edits.
STAGE_DEFAULTS = {
    "foundational": {"classes": [1, 2], "sections": [
        {"label": "Section A", "percent": 40, "kind": "objective: pick the answer, fill in the blank, match"},
        {"label": "Section B", "percent": 30, "kind": "very short answers"},
        {"label": "Section C", "percent": 30, "kind": "activity or picture-based questions"}]},
    "preparatory": {"classes": [3, 4, 5], "sections": [
        {"label": "Section A", "percent": 30, "kind": "objective questions"},
        {"label": "Section B", "percent": 40, "kind": "short answers"},
        {"label": "Section C", "percent": 30, "kind": "long answers and application questions"}]},
    "middle": {"classes": [6, 7, 8], "sections": [
        {"label": "Section A", "percent": 25, "kind": "objective questions including assertion-reason"},
        {"label": "Section B", "percent": 25, "kind": "short answers"},
        {"label": "Section C", "percent": 30, "kind": "long answers"},
        {"label": "Section D", "percent": 20, "kind": "case-based questions"}]},
}


def pattern_marks(pattern) -> int:
    return sum(marks * count for marks, count in pattern)


def pattern_count(pattern) -> int:
    return sum(count for _, count in pattern)


def describe_pattern(pattern) -> str:
    return ", ".join(f"{count} x {marks} mark{'s' if marks != 1 else ''}" for marks, count in pattern)


def _sections_for(entry):
    sections = deepcopy(entry["sections"])
    for sec in sections:
        sec["bloomLevels"] = []
        if "pattern" in sec:
            sec["percent"] = round(pattern_marks(sec["pattern"]) * 100 / entry["total"], 2)
    return sections


def get_blueprint(key):
    """A copy of one board blueprint as {code, class, subject, total, duration, sections}, or None."""
    entry = BLUEPRINTS.get(key) if isinstance(key, str) else None
    if not entry:
        return None
    for sec in entry["sections"]:
        if pattern_marks(sec["pattern"]) <= 0:
            raise ValueError(f"empty pattern in {key}")
    return {**{k: v for k, v in entry.items() if k != "sections"}, "key": key, "sections": _sections_for(entry)}


def stage_for_class(class_level):
    for name, data in STAGE_DEFAULTS.items():
        if class_level in data["classes"]:
            return name
    return None


def catalog():
    """What the paper form needs: board blueprints (classes 9-12) and editable starting sections (1-8)."""
    boards = []
    for key in BLUEPRINTS:
        bp = get_blueprint(key)
        boards.append({
            "key": key, "code": bp["code"], "class": bp["class"], "subject": bp["subject"],
            "total": bp["total"], "duration": bp["duration"], "competencyPercent": COMPETENCY_PERCENT,
            "sections": [{"label": s["label"], "marks": pattern_marks(s["pattern"]),
                          "questions": pattern_count(s["pattern"]), "kind": s["kind"]} for s in bp["sections"]],
        })
    return {"boards": boards, "stages": STAGE_DEFAULTS}
