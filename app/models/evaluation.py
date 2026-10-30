from datetime import datetime, timezone
from typing import Any, Dict, List
from bson import ObjectId
import math
from app.models.exam_validation import finite_number, nonblank_text


def _validate_parameter(p: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(p, dict):
        raise ValueError("Each parameter must be an object")
    name = nonblank_text(p.get("name"), "Parameter name")

    percentage = finite_number(p.get("percentage", 0), "Parameter percentage")
    if percentage < 0 or percentage > 100:
        raise ValueError("Parameter percentage must be between 0 and 100")

    return {"name": name, "percentage": percentage, "isCustom": bool(p.get("isCustom", False))}


def _validate_co(co: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(co, dict):
        raise ValueError("Each CO must be an object")
    code = nonblank_text(co.get("co_code"), "co_code")

    marks = finite_number(co.get("marks", 0), "CO marks")
    if marks < 0:
        raise ValueError("CO marks cannot be negative")

    description = co.get("description", "")
    if not isinstance(description, str):
        raise ValueError("CO description must be text")
    return {"co_code": code, "description": description.strip(), "marks": marks}


def _validate_question(q: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(q, dict) or "maxMarks" not in q:
        raise ValueError("maxMarks is required")

    max_marks = finite_number(q["maxMarks"], "maxMarks")
    min_marks = finite_number(q.get("minMarks", 0), "minMarks")

    if max_marks <= 0:
        raise ValueError("maxMarks must be greater than 0")
    if min_marks < 0:
        raise ValueError("minMarks cannot be negative")
    if min_marks > max_marks:
        raise ValueError("minMarks cannot be greater than maxMarks")

    parameters = q.get("parameters", [])
    if not isinstance(parameters, list):
        raise ValueError("parameters must be a list")
    validated_params = [_validate_parameter(p) for p in parameters]
    names = [p["name"].casefold() for p in validated_params]
    if len(names) != len(set(names)):
        raise ValueError("Parameter names must be unique within a question")
    if validated_params and not math.isclose(sum(p["percentage"] for p in validated_params), 100, rel_tol=0, abs_tol=1e-7):
        raise ValueError("Parameter percentages must total 100 for each question")

    cos = q.get("cos", [])
    if not isinstance(cos, list):
        raise ValueError("cos must be a list")
    validated_cos = []
    if cos:
        if not isinstance(cos, list):
            raise ValueError("cos must be a list")
        validated_cos = [_validate_co(co) for co in cos]
        codes = [co["co_code"] for co in validated_cos]
        if len(codes) != len(set(codes)):
            raise ValueError("CO codes must be unique within a question")
        if not math.isclose(sum(co["marks"] for co in validated_cos), max_marks, rel_tol=0, abs_tol=1e-7):
            raise ValueError("CO marks must total the question maxMarks")

    guidelines = q.get("guidelines", "")
    if not isinstance(guidelines, str):
        raise ValueError("guidelines must be text")

    result = {
        "minMarks": min_marks,
        "maxMarks": max_marks,
        "guidelines": guidelines.strip(),
        "parameters": validated_params,
        "cos": validated_cos,
    }
    # Optional source identity preserves legacy rubrics while retaining printed
    # labels and exact source blocks on new mapped rubrics used by AI grading.
    for key, limit in (("question_id", 64), ("question_label", 120), ("section", 200), ("question_text", 80000)):
        if key in q:
            value = q[key]
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f"{key} must be text up to {limit} characters")
            result[key] = value
    return result


def validate_evaluation_details(details: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(details, list) or not details:
        raise ValueError("questionEvaluationDetails must be a non-empty list")

    return [_validate_question(q) for q in details]


def _validate_total_marks(total_marks: Any, questions: List[Dict[str, Any]]) -> float:
    if total_marks is None:
        raise ValueError("totalMarks is required")

    total_marks = finite_number(total_marks, "totalMarks")
    if total_marks <= 0:
        raise ValueError("totalMarks must be greater than 0")

    sum_of_questions = sum(q["maxMarks"] for q in questions)
    if not math.isclose(sum_of_questions, total_marks, rel_tol=0, abs_tol=1e-7):
        raise ValueError("Sum of question maxMarks must equal totalMarks")

    return total_marks


def create_evaluation_detail_document(data: Dict[str, Any]) -> Dict[str, Any]:
    evaluation_details = validate_evaluation_details(data.get("questionEvaluationDetails", []))

    faculty_id = data.get("faculty_id")
    exam_id = data.get("exam_id")

    if not faculty_id or not ObjectId.is_valid(faculty_id):
        raise ValueError("Valid faculty_id is required")
    if not exam_id or not ObjectId.is_valid(exam_id):
        raise ValueError("Valid exam_id is required")

    total_marks = _validate_total_marks(data.get("totalMarks"), evaluation_details)
    now = datetime.now(timezone.utc)

    return {
        "faculty_id": ObjectId(faculty_id),
        "exam_id": ObjectId(exam_id),
        "totalMarks": total_marks,
        "questionEvaluationDetails": evaluation_details,
        "created_at": now,
        "updated_at": now,
    }


def update_evaluation_detail_document(data: Dict[str, Any]) -> Dict[str, Any]:
    update_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}

    evaluation_details = None
    if "questionEvaluationDetails" in data:
        evaluation_details = validate_evaluation_details(data["questionEvaluationDetails"])
        update_fields["questionEvaluationDetails"] = evaluation_details

    if "totalMarks" in data:
        questions_for_validation = (
            evaluation_details if evaluation_details is not None else data.get("questionEvaluationDetails", [])
        )
        update_fields["totalMarks"] = _validate_total_marks(data["totalMarks"], questions_for_validation)

    if "faculty_id" in data:
        if not ObjectId.is_valid(data["faculty_id"]):
            raise ValueError("Invalid faculty_id")
        update_fields["faculty_id"] = ObjectId(data["faculty_id"])

    if "exam_id" in data:
        if not ObjectId.is_valid(data["exam_id"]):
            raise ValueError("Invalid exam_id")
        update_fields["exam_id"] = ObjectId(data["exam_id"])

    return {"$set": update_fields}


def serialize_evaluation_detail(doc: Dict[str, Any]) -> Dict[str, Any] | None:
    if not doc:
        return None

    return {
        "id": str(doc["_id"]),
        "faculty_id": str(doc.get("faculty_id")) if doc.get("faculty_id") else None,
        "exam_id": str(doc.get("exam_id")) if doc.get("exam_id") else None,
        "totalMarks": doc.get("totalMarks"),
        "questionEvaluationDetails": doc.get("questionEvaluationDetails"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }
