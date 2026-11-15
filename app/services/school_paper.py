"""School-only parts of paper generation.

Applying a CBSE blueprint (with the teacher's edits) and the CBSE wording given to the AI live here, so the paper router
has no CBSE rules of its own. Colleges never reach this module: a college request carries no CBSE preset.
The blueprints themselves are in `cbse_blueprints`, the figure drawing in `math_figures`, `physics_figures` and `library_figures`.
"""
import json
from dataclasses import dataclass
from typing import Any, Dict

from app.services.cbse_blueprints import COMPETENCY_PERCENT, LayoutError, custom_sections, get_blueprint, is_customised


class SchoolPaperError(Exception):
    """A school paper request that cannot be used; `message` is shown to the teacher."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass
class SchoolPaper:
    """What a CBSE preset decides for a paper: the board blueprint plus the form fields it overrides."""
    board: Dict[str, Any]
    total_marks: int
    duration: str
    semester: str
    subject_name: str


def apply_cbse_preset(preset: str, layout_json: str | None, *, institute_type: str, total_marks: int,
                      duration: str, semester: str, subject_name: str) -> SchoolPaper:
    """The paper settings for a CBSE blueprint, with the teacher's edited layout when one was sent.

    Raises SchoolPaperError for an unknown blueprint, a non-school institute or an unusable layout.
    """
    board = get_blueprint(preset)
    if not board:
        raise SchoolPaperError(400, "Unknown CBSE blueprint")
    if institute_type != "school":
        raise SchoolPaperError(400, "CBSE blueprints are only for school institutes")

    board["customised"] = False
    if layout_json:
        try:
            layout = json.loads(layout_json)
            board["sections"] = custom_sections(preset, layout, total_marks)
        except (LayoutError, ValueError) as exc:
            raise SchoolPaperError(400, f"CBSE layout: {exc}")
        board["customised"] = is_customised(preset, layout, total_marks)
        board["total"] = total_marks

    return SchoolPaper(
        board=board,
        total_marks=board["total"],
        duration=(duration or "").strip()[:30] or board["duration"],
        semester=semester or f"Class {board['class']}",
        subject_name=board["subject"] if subject_name in ("", "Subject") else subject_name,
    )


def board_instructions(board: Dict[str, Any]) -> str:
    """The opening instruction for the AI when the paper follows a CBSE blueprint."""
    text = (
        f"Generate a CBSE Class {board['class']} {board['subject']} (subject code {board['code']}) examination paper "
        f"in the style of the CBSE 2026-27 sample question paper. Keep roughly {COMPETENCY_PERCENT}% of the marks on "
        "competency-focused questions (application, analysis, real-life and case-based). Write internal choices "
        "(an OR alternative of equal marks) inside the question text as the section rules say; the number of questions "
        "and attempt_count stay as the section rules state. Case-based questions state the passage first, then sub-parts."
    )
    if board.get("customised"):
        text += (" The teacher has changed the section layout from the official sample paper, so follow the "
                 "section rules below exactly, even where they differ from the official paper.")
    return text
