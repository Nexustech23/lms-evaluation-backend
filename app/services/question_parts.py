"""Parts inside one question: "Q1 has ten parts, 2 marks each".

A rubric row may carry `parts` (label, maxMarks, guidelines) and optional `partGroups` (the same
"answer N of these" groups used between questions, here between parts, for example "any 10 of 11"
or "one of the two (ii) alternatives"). The row's maxMarks is the counted maximum of its parts.

The AI marks every part; the application picks which parts count (best N of each group, a tie to the
earlier part) and the question's mark is the sum of the counted parts. A row without parts is untouched.
Pure functions, no I/O.
"""
import math

from app.services import choice_groups as cg

MAX_PARTS = 60


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_parts(parts, part_groups, row_max=None):
    """Normalise a row's parts and groups, or raise ValueError. Returns (parts, groups)."""
    if parts in (None, []):
        if part_groups:
            raise ValueError("partGroups need parts")
        return [], []
    if not isinstance(parts, list) or not 2 <= len(parts) <= MAX_PARTS:
        raise ValueError(f"A question with parts needs between 2 and {MAX_PARTS} parts")
    clean, labels = [], set()
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError("Each part must be an object")
        label = part.get("label")
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > 40:
            raise ValueError("Each part needs a short label such as (i)")
        label = label.strip()
        if label.casefold() in labels:
            raise ValueError(f"Part label {label!r} is used twice")
        labels.add(label.casefold())
        marks = part.get("maxMarks")
        if not _is_number(marks) or marks <= 0:
            raise ValueError(f"Part {label}: max marks must be greater than 0")
        guidelines = part.get("guidelines", "")
        if not isinstance(guidelines, str) or len(guidelines) > 3000:
            raise ValueError(f"Part {label}: guidelines must be text up to 3000 characters")
        clean.append({"label": label, "maxMarks": float(marks), "guidelines": guidelines.strip()})
    groups = cg.validate_groups(part_groups, clean)
    maximum = cg.counted_max(clean, groups)
    if row_max is not None and not math.isclose(maximum, float(row_max), rel_tol=0, abs_tol=1e-7):
        raise ValueError(f"The question's max marks must equal the counted marks of its parts ({maximum:g})")
    return clean, groups


def mark_parts(parts, part_groups, ai_parts):
    """Match the AI's per-part marks to the rubric parts and pick the counted ones.

    Returns (stored_parts, awarded) where `awarded` is the sum of the counted parts' marks.
    Raises ValueError with a plain message when the AI's parts do not match the rubric."""
    if not isinstance(ai_parts, list):
        raise ValueError("the answer must include a mark for every part")
    by_label = {}
    for item in ai_parts:
        label = item.get("label") if isinstance(item, dict) else None
        if not isinstance(label, str) or label.strip().casefold() in by_label:
            raise ValueError("each part must appear once with its label")
        by_label[label.strip().casefold()] = item
    if set(by_label) != {p["label"].casefold() for p in parts}:
        raise ValueError("the parts returned do not match the rubric parts")
    entries = []
    for number, part in enumerate(parts, 1):
        item = by_label[part["label"].casefold()]
        marks = item.get("ai_awarded_marks")
        if not _is_number(marks) or not 0 <= marks <= part["maxMarks"] + 1e-9:
            raise ValueError(f"part {part['label']} marks must be between 0 and {part['maxMarks']:g}")
        reasoning = item.get("reasoning", "")
        entries.append({"question_no": number, "label": part["label"], "max_marks": part["maxMarks"],
                        "ai_awarded_marks": float(marks), "final_marks": float(marks),
                        "reasoning": reasoning if isinstance(reasoning, str) else ""})
    cg.attach_groups(entries, part_groups)
    cg.reselect_counted(entries)
    awarded = cg.counted_sum(entries, "ai_awarded_marks")
    stored = []
    for entry in entries:
        out = {"part_no": entry["question_no"], "label": entry["label"], "max_marks": entry["max_marks"],
               "ai_awarded_marks": entry["ai_awarded_marks"], "reasoning": entry["reasoning"]}
        if "choice_group" in entry:
            out["choice_group"] = entry["choice_group"]
            out["counted"] = entry["counted"]
        stored.append(out)
    return stored, round(awarded, 6)


def counted_parts(parts):
    return [p for p in parts or [] if p.get("counted", True) is not False]
