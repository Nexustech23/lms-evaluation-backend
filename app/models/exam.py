from datetime import datetime, timezone
from typing import Any, Dict
from bson import ObjectId


def validate_exam_data(data: Dict[str, Any]) -> bool:
    required_object_ids = ["school_id", "programme_id", "subject_id", "faculty_id"]

    for field in required_object_ids:
        if not data.get(field) or not ObjectId.is_valid(str(data[field])):
            raise ValueError(f"Invalid or missing {field}")

    if not data.get("folder_name"):
        raise ValueError("folder_name is required")
    if not data.get("exam_type"):
        raise ValueError("exam_type is required")
    if not data.get("semester"):
        raise ValueError("semester is required")

    if "covered_cos" in data and not isinstance(data["covered_cos"], list):
        raise ValueError("covered_cos must be a list")

    if "weightage" in data:
        try:
            int(data["weightage"])
        except (TypeError, ValueError):
            raise ValueError("weightage must be a number")

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

        "semester": int(data["semester"]),

        "covered_cos": data.get("covered_cos", []),
        "is_course_exit_summary": bool(data.get("is_course_exit_summary", False)),
        "weightage": int(data.get("weightage", 0)),
        "exam_title": data.get("exam_title"),
        "exam_type": data.get("exam_type"),
        "exam_date": datetime.fromisoformat(data["exam_date"]) if data.get("exam_date") else None,

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
        if field in data and data[field] and ObjectId.is_valid(str(data[field])):
            update_fields[field] = ObjectId(data[field])

    simple_fields = ["folder_name", "exam_title", "exam_type", "semester", "is_archived"]
    for field in simple_fields:
        if field in data:
            update_fields[field] = data[field]

    if "exam_date" in data and data["exam_date"]:
        update_fields["exam_date"] = datetime.fromisoformat(data["exam_date"])

    if "covered_cos" in data:
        if not isinstance(data["covered_cos"], list):
            raise ValueError("covered_cos must be a list")
        update_fields["covered_cos"] = data["covered_cos"]

    if "weightage" in data:
        update_fields["weightage"] = int(data["weightage"])
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
        update_fields["is_course_exit_summary"] = data["is_course_exit_summary"] in [True, "true", "True", 1, "1"]

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
