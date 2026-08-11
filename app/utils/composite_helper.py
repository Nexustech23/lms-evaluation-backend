from typing import Any, Dict, List, Optional


def compute_weighted_composite(exam_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Combine a student's per-exam percentages into one weighted composite,
    using each exam's configured weightage (0-100).

    exam_results: list of {"exam_id", "weightage": int, "percentage": float|None}
        percentage is final_marks/max_marks*100 for that exam; None means the
        student has no answer sheet for that exam at all (not the same as a
        real 0).

    Normalizes by the weight actually present (weight_used), not a literal
    100, since exam weightages aren't validated to sum to 100 today and a
    composite is inherently partial mid-semester (e.g. CA1+CA2 graded,
    End-Term not yet).
    """
    weight_expected = sum(float(e.get("weightage", 0) or 0) for e in exam_results)
    attempted = [e for e in exam_results if e.get("percentage") is not None]
    weight_used = sum(float(e.get("weightage", 0) or 0) for e in attempted)

    if weight_used <= 0:
        return {
            "composite_percentage": None,
            "weight_used": 0.0,
            "weight_expected": weight_expected,
            "coverage_ratio": 0.0,
            "is_partial": True,
        }

    weighted_sum = sum(float(e["percentage"]) * float(e.get("weightage", 0) or 0) for e in attempted)
    composite_percentage = round(weighted_sum / weight_used, 2)
    coverage_ratio = round(weight_used / weight_expected, 4) if weight_expected else 0.0

    return {
        "composite_percentage": composite_percentage,
        "weight_used": weight_used,
        "weight_expected": weight_expected,
        "coverage_ratio": coverage_ratio,
        "is_partial": coverage_ratio < 1.0,
    }
