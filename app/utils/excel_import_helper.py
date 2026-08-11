# ============================================================
# Bulk marks Excel parser — ported from utils/excel_import_helper.py.
# Pure computation, no I/O beyond the pandas read itself.
# ============================================================

import math
import re
from typing import Any, Dict, List, Optional

import pandas as pd

COLUMN_ALIASES = {
    "student_name": [
        "student_name", "student name", "name of the student", "name",
        "name_of_student", "name_of_the_student", "student",
    ],
    "marks": [
        "marks", "total", "total marks", "total_marks", "total (out of 100)",
        "overall marks", "normalized total", "normalized total (100)",
        "normalized_total_100", "score", "obtained marks",
    ],
}


def normalize(name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def _find_column(columns: List[str], field: str) -> Optional[str]:
    aliases = {normalize(alias) for alias in COLUMN_ALIASES[field]}
    for column in columns:
        if column in aliases:
            return column

    for column in columns:
        tokens = set(column.split("_"))
        if field == "student_name" and ({"student", "name"} & tokens):
            return column
        if field == "marks" and ({"mark", "marks", "total", "score"} & tokens):
            return column

    return None


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip() == ""


def _student_name(value: Any, fallback: str) -> str:
    if _is_blank(value):
        return fallback
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text or fallback


def read_marks_excel(file) -> List[Dict[str, Any]]:
    """
    file: a file-like object (bytes buffer) containing an .xlsx workbook.
    Returns a list of {"student_name": str, "marks": float}. Raises ValueError
    on any structural problem (matches Flask's error-message text exactly,
    since these surface directly to the caller).
    """
    try:
        df = pd.read_excel(file)
    except Exception as e:
        raise ValueError(f"Unable to read Excel file: {e}") from e

    normalized_columns = [normalize(c) for c in df.columns]
    df.columns = normalized_columns

    name_column = _find_column(list(df.columns), "student_name")
    marks_column = _find_column(list(df.columns), "marks")

    if marks_column is None:
        raise ValueError("Could not find a marks column in the uploaded Excel file")

    students: List[Dict[str, Any]] = []

    for row_number, (_, row) in enumerate(df.iterrows(), start=2):
        name_value = row[name_column] if name_column else None
        marks_value = row[marks_column]

        if _is_blank(name_value) and _is_blank(marks_value):
            continue

        student_name = _student_name(name_value, f"Student {row_number - 1}")

        try:
            marks = float(marks_value)
        except (TypeError, ValueError):
            raise ValueError(f"Marks must be a number (invalid Excel row: {row_number})")

        if not (0 <= marks <= 100):
            raise ValueError(f"Marks must be between 0 and 100 (invalid Excel row: {row_number})")

        students.append({"student_name": student_name, "marks": marks})

    if not students:
        raise ValueError("The Excel file does not contain any valid marks rows")

    return students
