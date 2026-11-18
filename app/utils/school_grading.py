# ============================================================
# School grading. A school grades each student by their own marks: the percentage falls in a band and the band is the
# grade. There is no ranking, so a grade never depends on who else sat the exam. The bands are the CBSE nine-point scale
# until the school edits them (the board's own national result ranks passed candidates, which a school cannot repeat for
# its own exams). A college is not touched: it keeps its relative grading.
# ============================================================

import copy
import math
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_PASS_PERCENTAGE = 33.0
MAX_BANDS = 20
MAX_GRADE_LENGTH = 8
MAX_GRADE_POINT = 10.0

# The CBSE nine-point scale, by marks. Highest band first.
DEFAULT_BANDS: List[Dict[str, Any]] = [
    {"grade": "A1", "min_percentage": 91.0, "grade_point": 10.0},
    {"grade": "A2", "min_percentage": 81.0, "grade_point": 9.0},
    {"grade": "B1", "min_percentage": 71.0, "grade_point": 8.0},
    {"grade": "B2", "min_percentage": 61.0, "grade_point": 7.0},
    {"grade": "C1", "min_percentage": 51.0, "grade_point": 6.0},
    {"grade": "C2", "min_percentage": 41.0, "grade_point": 5.0},
    {"grade": "D", "min_percentage": 33.0, "grade_point": 4.0},
    {"grade": "E1", "min_percentage": 21.0, "grade_point": 0.0},
    {"grade": "E2", "min_percentage": 0.0, "grade_point": 0.0},
]


def default_config() -> Dict[str, Any]:
    return {"pass_percentage": DEFAULT_PASS_PERCENTAGE, "bands": copy.deepcopy(DEFAULT_BANDS)}


def _number(value: Any, label: str, low: float, high: float) -> Tuple[Optional[float], Optional[str]]:
    if isinstance(value, bool) or value is None or value == "":
        return None, f"{label} must be a number"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, f"{label} must be a number"
    if not math.isfinite(number):
        return None, f"{label} must be a number"
    if number < low or number > high:
        return None, f"{label} must be between {low:g} and {high:g}"
    return round(number, 2), None


def validate_config(data: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """The cleaned grading setup, or the reason it cannot be used. Bands come back highest first."""
    if not isinstance(data, dict):
        return None, "Grading setup must be an object"

    pass_percentage, error = _number(data.get("pass_percentage", DEFAULT_PASS_PERCENTAGE), "Pass mark", 0, 100)
    if error:
        return None, error

    raw_bands = data.get("bands")
    if not isinstance(raw_bands, list) or not raw_bands:
        return None, "Add at least one grade"
    if len(raw_bands) > MAX_BANDS:
        return None, f"Use at most {MAX_BANDS} grades"

    bands, seen = [], set()
    for position, raw in enumerate(raw_bands, start=1):
        if not isinstance(raw, dict):
            return None, f"Grade {position} is not valid"
        grade = raw.get("grade")
        grade = grade.strip() if isinstance(grade, str) else ""
        if not grade:
            return None, f"Grade {position} needs a name"
        if len(grade) > MAX_GRADE_LENGTH:
            return None, f"Grade name '{grade}' is longer than {MAX_GRADE_LENGTH} characters"
        if grade.casefold() in seen:
            return None, f"Grade '{grade}' is listed twice"
        seen.add(grade.casefold())

        minimum, error = _number(raw.get("min_percentage"), f"Lowest marks for {grade}", 0, 100)
        if error:
            return None, error
        point, error = _number(raw.get("grade_point"), f"Grade point for {grade}", 0, MAX_GRADE_POINT)
        if error:
            return None, error
        bands.append({"grade": grade, "min_percentage": minimum, "grade_point": point})

    bands.sort(key=lambda band: band["min_percentage"], reverse=True)
    for higher, lower in zip(bands, bands[1:]):
        if higher["min_percentage"] == lower["min_percentage"]:
            return None, f"{higher['grade']} and {lower['grade']} start at the same marks"
    if bands[-1]["min_percentage"] != 0:
        return None, f"The lowest grade ({bands[-1]['grade']}) must start at 0 so every mark has a grade"

    return {"pass_percentage": pass_percentage, "bands": bands}, None


def config_from_doc(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A saved setup, or the CBSE default when none is saved or the saved one is no longer valid."""
    if doc:
        config, error = validate_config({"pass_percentage": doc.get("pass_percentage"), "bands": doc.get("bands")})
        if not error:
            return config
    return default_config()


def grade_for(percentage: Any, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The band a percentage falls in (highest band whose lowest marks it reaches), or None without a percentage."""
    if percentage is None or isinstance(percentage, bool):
        return None
    try:
        value = float(percentage)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    for band in config["bands"]:
        if value >= band["min_percentage"]:
            return band
    return config["bands"][-1]


def is_pass(percentage: Any, config: Dict[str, Any]) -> bool:
    try:
        return float(percentage) >= config["pass_percentage"]
    except (TypeError, ValueError):
        return False


def assign_school_grades(students: List[Dict[str, Any]], config: Dict[str, Any], max_marks: float = 100) -> List[Dict[str, Any]]:
    """Sets grade on every student from their own overall_total out of max_marks. Only `grade` is added, exactly as the
    college path does: the export treats any other key on a row as a subject column."""
    for student in students:
        total = student.get("overall_total", 0) or 0
        percentage = (total / max_marks * 100) if max_marks else 0
        student["grade"] = grade_for(percentage, config)["grade"]
    return students


async def config_if_school(db, institute_id) -> Optional[Dict[str, Any]]:
    """The institute's grading setup when it is a school, None for a college (which keeps relative grading)."""
    institute = await db["instituteDetails"].find_one({"_id": institute_id}, {"institute_type": 1})
    if not institute or institute.get("institute_type") != "school":
        return None
    doc = await db["schoolGradings"].find_one({"institute_id": institute_id})
    return config_from_doc(doc)
