from datetime import datetime, timezone
from typing import Any, Dict, List
from bson import ObjectId


def _validate_cos(cos_list: List[Dict[str, Any]], question_max_marks: float):
    if not isinstance(cos_list, list):
        raise ValueError("cos must be a list")

    validated = []
    total_co_final = 0

    for co in cos_list:
        if "co_code" not in co:
            raise ValueError("co_code is required in cos")

        ai_marks = float(co.get("ai_marks", 0))
        grace_marks = float(co.get("grace_marks", 0))
        max_marks = float(co.get("max_marks", 0))

        if max_marks <= 0:
            raise ValueError("co max_marks must be greater than 0")
        if ai_marks < 0:
            raise ValueError("ai_marks cannot be negative")
        if ai_marks > max_marks:
            raise ValueError(f"ai_marks cannot exceed co max_marks for {co['co_code']}")

        final_co_marks = ai_marks + grace_marks
        if final_co_marks < 0:
            final_co_marks = 0
        if final_co_marks > max_marks:
            final_co_marks = max_marks

        total_co_final += final_co_marks

        validated.append({
            "co_code": co["co_code"],
            "ai_marks": ai_marks,
            "grace_marks": grace_marks,
            "max_marks": max_marks,
            "final_co_marks": final_co_marks,
            "remarks": co.get("remarks"),
        })

    if total_co_final > question_max_marks:
        raise ValueError("Total CO final marks cannot exceed question max_marks")

    return validated


def _validate_question(q: Dict[str, Any]) -> Dict[str, Any]:
    for field in ["question_no", "max_marks"]:
        if field not in q:
            raise ValueError(f"{field} missing in questionwise_marking")

    question_no = int(q["question_no"])
    max_marks = float(q["max_marks"])

    ai_awarded = float(q.get("ai_awarded_marks", 0))
    grace_marks = float(q.get("grace_marks", 0))

    if max_marks <= 0:
        raise ValueError("max_marks must be greater than 0")
    if ai_awarded < 0:
        raise ValueError("ai_awarded_marks cannot be negative")
    if ai_awarded > max_marks:
        raise ValueError("ai_awarded_marks cannot exceed max_marks")

    final_marks = ai_awarded + grace_marks
    if final_marks < 0:
        final_marks = 0
    if final_marks > max_marks:
        final_marks = max_marks

    validated_cos = []
    if "cos" in q and q["cos"]:
        validated_cos = _validate_cos(q["cos"], max_marks)

    return {
        "question_no": question_no,
        "max_marks": max_marks,
        "ai_awarded_marks": ai_awarded,
        "grace_marks": grace_marks,
        "final_marks": final_marks,
        "parameters": q.get("parameters", []),
        "cos": validated_cos,
        "reasoning": q.get("reasoning"),
        "feedback": q.get("feedback"),
        "improvement": q.get("improvement"),
        "flags": q.get("flags", {}),
    }


def validate_questionwise(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(data, list) or not data:
        raise ValueError("questionwise_marking must be a non-empty list")

    return [_validate_question(q) for q in data]


def _validate_token_usage(token_usage: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(token_usage, dict):
        raise ValueError("token_usage must be a dict")

    gemini_raw = token_usage.get("gemini", {})
    gemini = {
        "calls": gemini_raw.get("calls", []),
        "total_prompt_tokens": int(gemini_raw.get("total_prompt_tokens", 0)),
        "total_candidate_tokens": int(gemini_raw.get("total_candidate_tokens", 0)),
        "total_tokens": int(gemini_raw.get("total_tokens", 0)),
    }

    claude_raw = token_usage.get("claude", {})
    claude = {
        "calls": claude_raw.get("calls", []),
        "total_input_tokens": int(claude_raw.get("total_input_tokens", 0)),
        "total_output_tokens": int(claude_raw.get("total_output_tokens", 0)),
        "total_tokens": int(claude_raw.get("total_tokens", 0)),
    }

    grand_total = int(token_usage.get("grand_total_tokens", 0))
    expected_grand = gemini["total_tokens"] + claude["total_tokens"]
    if grand_total != expected_grand:
        grand_total = expected_grand

    recorded_at = token_usage.get("recorded_at")
    if not isinstance(recorded_at, datetime):
        recorded_at = datetime.now(timezone.utc)

    return {"gemini": gemini, "claude": claude, "grand_total_tokens": grand_total, "recorded_at": recorded_at}


def _empty_token_usage() -> Dict[str, Any]:
    return {
        "gemini": {"calls": [], "total_prompt_tokens": 0, "total_candidate_tokens": 0, "total_tokens": 0},
        "claude": {"calls": [], "total_input_tokens": 0, "total_output_tokens": 0, "total_tokens": 0},
        "grand_total_tokens": 0,
        "recorded_at": None,
    }


def create_initial_answer_document(data: Dict[str, Any]) -> Dict[str, Any]:
    exam_id = data.get("exam_id")
    faculty_id = data.get("faculty_id")

    if not ObjectId.is_valid(exam_id):
        raise ValueError("Invalid exam_id")
    if not ObjectId.is_valid(faculty_id):
        raise ValueError("Invalid faculty_id")

    now = datetime.now(timezone.utc)

    return {
        "exam_id": ObjectId(exam_id),
        "faculty_id": ObjectId(faculty_id),

        "filename": data.get("filename"),
        "answer_script_url": data.get("answer_script_url"),
        "fileId": data.get("fileId"),

        "questionwise_marking": [],

        "total_ai_marks": 0,
        "total_final_marks": 0,
        "total_max_marks": 0,

        "reviewed_by_professor": False,

        "evaluated_report_url": None,
        "evaluated_report_fileId": None,
        "html_content": None,
        "transcript_pdf_fileId": None,

        "student_name": None,

        "token_usage": _empty_token_usage(),

        "evaluated_at": None,
        "created_at": now,
        "updated_at": now,
    }


def update_answer_evaluation_document(data: Dict[str, Any]) -> Dict[str, Any]:
    update_fields = {"updated_at": datetime.now(timezone.utc)}

    if "questionwise_marking" in data:
        questionwise = validate_questionwise(data["questionwise_marking"])
        update_fields["questionwise_marking"] = questionwise

        update_fields["total_final_marks"] = sum(q["final_marks"] for q in questionwise)
        update_fields["total_ai_marks"] = sum(q["ai_awarded_marks"] for q in questionwise)
        update_fields["total_max_marks"] = sum(q["max_marks"] for q in questionwise)
        update_fields["evaluated_at"] = datetime.now(timezone.utc)

    if "token_usage" in data:
        update_fields["token_usage"] = _validate_token_usage(data["token_usage"])

    if "reviewed_by_professor" in data:
        update_fields["reviewed_by_professor"] = bool(data["reviewed_by_professor"])

    if "evaluated_report_url" in data:
        update_fields["evaluated_report_url"] = data["evaluated_report_url"]

    if "evaluated_report_fileId" in data:
        update_fields["evaluated_report_fileId"] = data["evaluated_report_fileId"]

    if "html_content" in data:
        update_fields["html_content"] = data["html_content"]

    if "transcript_pdf_fileId" in data:
        update_fields["transcript_pdf_fileId"] = data["transcript_pdf_fileId"]

    if "student_name" in data:
        update_fields["student_name"] = data["student_name"]

    return {"$set": update_fields}
