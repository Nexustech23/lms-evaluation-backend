from datetime import datetime, timezone
from typing import Any, Dict
from bson import ObjectId
from app.models.exam_validation import (
    boolean_value, covered_co_list, exam_datetime, nonblank_text,
    semester_number, weightage_number,
)


def validate_exam_data(data: Dict[str, Any]) -> bool:
    required_object_ids = ["school_id", "programme_id", "subject_id", "faculty_id"]

    for field in required_object_ids:
        if not data.get(field) or not ObjectId.is_valid(str(data[field])):
            raise ValueError(f"Invalid or missing {field}")

    nonblank_text(data.get("folder_name"), "folder_name")
    nonblank_text(data.get("exam_type"), "exam_type")
    if data.get("exam_title") is not None:
        nonblank_text(data["exam_title"], "exam_title")
    semester_number(data.get("semester"))
    covered_co_list(data.get("covered_cos"))
    weightage_number(data.get("weightage"))
    exam_datetime(data.get("exam_date"))
    for field in ("department_id", "batch_id"):
        if data.get(field) not in (None, "", "null") and not ObjectId.is_valid(str(data[field])):
            raise ValueError(f"Invalid {field}")

    return True


def create_exam_document(data: Dict[str, Any], created_by: ObjectId) -> Dict[str, Any]:
    validate_exam_data(data)
    now = datetime.now(timezone.utc)

    return {
        "folder_name": data["folder_name"].strip(),

        "created_by": created_by,
        "faculty_id": ObjectId(data["faculty_id"]),
        "school_id": ObjectId(data["school_id"]),
        "programme_id": ObjectId(data["programme_id"]),
        "department_id": (
            ObjectId(data["department_id"])
            if data.get("department_id") and ObjectId.is_valid(str(data["department_id"]))
            else None
        ),
        "batch_id": (
            ObjectId(data["batch_id"])
            if data.get("batch_id") and ObjectId.is_valid(str(data["batch_id"]))
            else None
        ),
        "subject_id": ObjectId(data["subject_id"]),

        "semester": semester_number(data["semester"]),

        "covered_cos": covered_co_list(data.get("covered_cos")),
        "is_course_exit_summary": boolean_value(data.get("is_course_exit_summary", False), "is_course_exit_summary"),
        "weightage": weightage_number(data.get("weightage")),
        "exam_title": nonblank_text(data["exam_title"], "exam_title") if data.get("exam_title") is not None else None,
        "exam_type": nonblank_text(data["exam_type"], "exam_type"),
        "exam_date": exam_datetime(data.get("exam_date")),

        "question_paper": {
            "url": None,
            "fileId": None,
            "filename": None,
            "no_of_questions": 0,
            "text": None,
        },

        "is_archived": False,
        "created_at": now,
        "updated_at": now,
    }


def update_exam_document(data: Dict[str, Any]) -> Dict[str, Any]:
    update_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}

    object_fields = ["school_id", "programme_id", "department_id", "batch_id", "subject_id"]
    for field in object_fields:
        if field in data:
            if field in ("department_id", "batch_id") and data[field] in (None, "", "null"):
                update_fields[field] = None
            elif ObjectId.is_valid(str(data[field])):
                update_fields[field] = ObjectId(str(data[field]))
            else:
                raise ValueError(f"Invalid {field}")

    simple_fields = ["folder_name", "exam_title", "exam_type"]
    for field in simple_fields:
        if field in data:
            update_fields[field] = nonblank_text(data[field], field)

    if "semester" in data:
        update_fields["semester"] = semester_number(data["semester"])
    if "exam_date" in data:
        if data["exam_date"] is None:
            raise ValueError("exam_date is required")
        update_fields["exam_date"] = exam_datetime(data["exam_date"])

    if "covered_cos" in data:
        update_fields["covered_cos"] = covered_co_list(data["covered_cos"])

    if "weightage" in data:
        update_fields["weightage"] = weightage_number(data["weightage"])
        if "question_paper" in data:
            qp = data["question_paper"]
            update_fields["question_paper"] = {
                "url": qp.get("url"),
                "fileId": qp.get("fileId"),
                "filename": qp.get("filename"),
                "no_of_questions": int(qp.get("no_of_questions", 0)),
                "text": qp.get("text"),
            }

    if "is_course_exit_summary" in data:
        update_fields["is_course_exit_summary"] = boolean_value(data["is_course_exit_summary"], "is_course_exit_summary")

    return {"$set": update_fields}


def serialize_exam(doc: Dict[str, Any]) -> Dict[str, Any] | None:
    if not doc:
        return None

    return {
        "id": str(doc["_id"]),

        "faculty_id": str(doc.get("faculty_id")) if doc.get("faculty_id") else None,
        "school_id": str(doc.get("school_id")) if doc.get("school_id") else None,
        "programme_id": str(doc.get("programme_id")) if doc.get("programme_id") else None,
        "department_id": str(doc.get("department_id")) if doc.get("department_id") else None,
        "batch_id": str(doc.get("batch_id")) if doc.get("batch_id") else None,
        "subject_id": str(doc.get("subject_id")) if doc.get("subject_id") else None,

        "semester": doc.get("semester"),

        "folder_name": doc.get("folder_name"),
        "exam_title": doc.get("exam_title"),
        "exam_type": doc.get("exam_type"),
        "exam_date": doc.get("exam_date").isoformat() if doc.get("exam_date") else None,
        "covered_cos": doc.get("covered_cos", []),
        "weightage": doc.get("weightage", 0),
        "is_course_exit_summary": doc.get("is_course_exit_summary", False),

        "question_paper": doc.get("question_paper"),

        "is_archived": doc.get("is_archived"),
        "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
        "updated_at": doc.get("updated_at").isoformat() if doc.get("updated_at") else None,
    }
