"""Validate a complete manual-entry batch before writing any student marks."""
from decimal import Decimal
from fractions import Fraction

from app.models.answer import validate_questionwise
from app.models.evaluation import validate_evaluation_details, _validate_total_marks
from app.models.exam_validation import finite_number


def allocate_co_marks(awarded, cos):
    active = [co for co in cos if co["marks"] > 0]
    if not active:
        return []
    values = [awarded] + [co["marks"] for co in active]
    places = max(2, *(-Decimal(str(value)).as_tuple().exponent for value in values))
    scale = 10 ** places
    target = int(Fraction(str(awarded)) * scale)
    maxima = [Fraction(str(co["marks"])) for co in active]
    total = sum(maxima)
    if Fraction(str(awarded)) > total:
        raise ValueError("CO allocations do not cover the awarded question marks; correct the rubric")
    exact = [target * maximum / total for maximum in maxima]
    units = [int(value) for value in exact]
    remainder = target - sum(units)
    order = sorted(range(len(active)), key=lambda i: exact[i] - units[i], reverse=True)
    for index in order[:remainder]:
        units[index] += 1
    return [{"co_code": co["co_code"], "ai_marks": float(Fraction(value, scale)),
             "grace_marks": 0, "max_marks": co["marks"], "remarks": "Manually entered"}
            for co, value in zip(active, units)]


def prepare_manual_marks(payload, evaluation):
    if not payload.entries:
        raise ValueError("At least one student entry is required")
    questions = evaluation.get("questionEvaluationDetails") if evaluation else None
    if questions:
        questions = validate_evaluation_details(questions)
        maximum = _validate_total_marks(evaluation.get("totalMarks"), questions)
    else:
        maximum = finite_number(payload.max_marks, "max_marks")
        if maximum <= 0:
            raise ValueError("max_marks must be greater than zero")
    prepared, seen = [], set()
    for row, entry in enumerate(payload.entries, 1):
        sid = entry.get("student_id")
        if isinstance(sid, bool) or not isinstance(sid, (str, int)) or not str(sid).strip():
            raise ValueError(f"Row {row}: student_id is required")
        sid = str(sid).strip()
        if sid in seen:
            raise ValueError(f"Row {row}: duplicate student_id")
        seen.add(sid)
        marking = []
        if questions:
            marks = entry.get("question_marks")
            if not isinstance(marks, list) or len(marks) != len(questions):
                raise ValueError(f"Row {row}: include every rubric question exactly once")
            by_number = {}
            for question in marks:
                if not isinstance(question, dict):
                    raise ValueError(f"Row {row}: each question must be an object")
                number = finite_number(question.get("question_no"), "question_no")
                if not number.is_integer() or not 1 <= number <= len(questions) or number in by_number:
                    raise ValueError(f"Row {row}: question numbers must be valid and unique")
                by_number[int(number)] = finite_number(question.get("marks"), "marks")
            for index, question in enumerate(questions, 1):
                awarded = by_number[index]
                if not 0 <= awarded <= question["maxMarks"]:
                    raise ValueError(f"Row {row}: question {index} marks are outside the allowed range")
                marking.append({"question_no": index, "max_marks": question["maxMarks"],
                    "ai_awarded_marks": awarded, "grace_marks": 0,
                    "cos": allocate_co_marks(awarded, question["cos"])})
            marking = validate_questionwise(marking)
            total = sum(q["final_marks"] for q in marking)
        else:
            total = finite_number(entry.get("marks"), "marks")
            if not 0 <= total <= maximum:
                raise ValueError(f"Row {row}: marks must be between zero and max_marks")
        prepared.append({"student_id": sid, "total_final_marks": total,
            "total_ai_marks": total, "total_max_marks": maximum, "questionwise_marking": marking})
    return prepared
