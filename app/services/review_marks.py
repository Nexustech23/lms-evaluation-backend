"""Validate faculty reviews against the saved answer's question limits."""

import math
from app.services.evaluation_review import optional_review

from app.models.answer import validate_questionwise


def _number(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label} must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _question_map(questions):
    if not isinstance(questions, list) or not questions:
        raise ValueError("A review requires a non-empty saved question evaluation")
    result = {}
    for question in questions:
        if not isinstance(question, dict):
            raise ValueError("Each question must be an object")
        number = _number(question.get("question_no"), "question_no")
        if number <= 0 or not number.is_integer() or number in result:
            raise ValueError("Question numbers must be positive, unique integers")
        result[int(number)] = question
    return result


def _co_map(cos):
    if not isinstance(cos, list):
        raise ValueError("cos must be a list")
    result = {}
    for co in cos:
        if not isinstance(co, dict) or not isinstance(co.get("co_code"), str):
            raise ValueError("Each CO must have a co_code")
        code = co["co_code"].strip()
        if not code or code in result:
            raise ValueError("CO codes must be non-empty and unique within a question")
        result[code] = co
    return result


def _marks(item, saved, ai_field):
    maximum = _number(saved.get("max_marks"), "Saved max_marks")
    submitted_maximum = _number(item.get("max_marks"), "max_marks")
    if maximum < 0 or submitted_maximum != maximum:
        raise ValueError("max_marks must match the saved evaluation")
    ai = _number(item.get(ai_field, 0), ai_field)
    if not math.isclose(ai, _number(saved.get(ai_field, 0), "Saved AI marks"), rel_tol=0, abs_tol=1e-7):
        raise ValueError("Original AI marks cannot change; use faculty adjustment")
    grace = _number(item.get("grace_marks", 0), "grace_marks")
    final = ai + grace
    if not 0 <= ai <= maximum or not 0 <= final <= maximum:
        raise ValueError("AI and final marks must be between zero and max_marks")
    return {**item, "max_marks": maximum, ai_field: ai, "grace_marks": grace}


def validate_review_marks(submitted, answer):
    saved = _question_map(answer.get("questionwise_marking"))
    reviewed = _question_map(submitted)
    if saved.keys() != reviewed.keys():
        raise ValueError("Review must contain every saved question exactly once")

    normalized = []
    for number, question in reviewed.items():
        original = saved[number]
        confirmed = question.get("review_confirmed") is True
        if original.get("flags", {}).get("needs_review"):
            changed = _number(question.get("grace_marks", 0), "grace_marks") != _number(original.get("grace_marks", 0), "Saved grace marks")
            old_cos = _co_map(original.get("cos") or [])
            new_cos = _co_map(question.get("cos") or [])
            changed = changed or any(
                _number(co.get("grace_marks", 0), "CO adjustment") != _number(old_cos.get(code, {}).get("grace_marks", 0), "Saved CO adjustment")
                for code, co in new_cos.items())
            if (not optional_review(answer) or changed or confirmed) and (
                    not confirmed or not isinstance(question.get("review_note"), str) or not question["review_note"].strip()):
                raise ValueError(f"Question {number}: inspect the original and explicitly confirm the review with a note")
        question = _marks(question, original, "ai_awarded_marks")
        question["question_no"] = number
        saved_cos = _co_map(original.get("cos") or [])
        reviewed_cos = _co_map(question.get("cos", []))
        if saved_cos.keys() != reviewed_cos.keys():
            raise ValueError("Review COs must match the saved evaluation")
        question["cos"] = [
            {**_marks(co, saved_cos[code], "ai_marks"), "co_code": code}
            for code, co in reviewed_cos.items()
        ]
        if question["cos"]:
            co_final = sum(co["ai_marks"] + co["grace_marks"] for co in question["cos"])
            final = question["ai_awarded_marks"] + question["grace_marks"]
            if not math.isclose(co_final, final, rel_tol=0, abs_tol=1e-7):
                raise ValueError("CO final marks must add up to the question final marks")
        normalized.append(question)

    validated = validate_questionwise(normalized)
    for q in validated:
        source = reviewed[q["question_no"]]
        original = saved[q["question_no"]]
        for field in ("evidence", "review_reasons", "proposed_ai_marks"):
            if field in original:
                q[field] = original[field]
        note = source.get("review_note") or ""
        if not isinstance(note, str):
            raise ValueError("Review note must be text")
        q["review_note"] = note.strip()
        q["review_confirmed"] = source.get("review_confirmed") is True
        q["flags"] = {**original.get("flags", {}), "needs_review": bool(original.get("flags", {}).get("needs_review") and not q["review_confirmed"])}
        if q["final_marks"] > 0:
            q["flags"]["unanswered"] = False
    total = sum(q["final_marks"] for q in validated)
    if not math.isfinite(total):
        raise ValueError("Final total must be finite")
    maximum = answer.get("total_max_marks")
    if maximum is not None:
        maximum = _number(maximum, "Saved total_max_marks")
        if maximum <= 0 or total > maximum:
            raise ValueError("Final total cannot exceed the saved answer maximum")
    return validated
