from typing import Any, Dict, List

from app.utils.grade_points import get_grade_point


def calculate_credit_points(credits: float, grade: str) -> float:
    """Credit Points = Credits x Grade Point"""
    return round(credits * get_grade_point(grade), 2)


def calculate_tgpa(subjects: List[Dict[str, Any]]) -> Dict[str, Any]:
    """subjects = [{"credits": 4, "grade": "A"}, ...]"""
    total_credits = 0
    total_credit_points = 0

    for subject in subjects:
        credits = subject.get("credits", 0)
        grade = subject.get("grade", "")
        total_credits += credits
        total_credit_points += calculate_credit_points(credits, grade)

    tgpa = round(total_credit_points / total_credits, 2) if total_credits else 0

    return {
        "total_credits": total_credits,
        "total_credit_points": round(total_credit_points, 2),
        "tgpa": tgpa,
    }


def calculate_cgpa(semester_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """semester_results = [{"total_credits": 10, "total_credit_points": 91.5}, ...]"""
    overall_credits = 0
    overall_credit_points = 0

    for semester in semester_results:
        overall_credits += semester.get("total_credits", 0)
        overall_credit_points += semester.get("total_credit_points", 0)

    cgpa = round(overall_credit_points / overall_credits, 2) if overall_credits else 0

    return {
        "overall_credits": overall_credits,
        "overall_credit_points": round(overall_credit_points, 2),
        "cgpa": cgpa,
    }
