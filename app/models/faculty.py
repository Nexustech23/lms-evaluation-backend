from datetime import datetime, timezone
from typing import Any, Dict
from bson import ObjectId


def validate_faculty_data(data: Dict[str, Any]):
    if not data:
        raise ValueError("No data provided")

    user_id = data.get("user_id")
    institute_id = data.get("institute_id")
    school_id = data.get("school_id")

    if not user_id or not ObjectId.is_valid(user_id):
        raise ValueError("Valid user_id is required")

    if not institute_id or not ObjectId.is_valid(institute_id):
        raise ValueError("Valid institute_id is required")

    if not school_id or not ObjectId.is_valid(school_id):
        raise ValueError("Valid school_id is required")

    return ObjectId(user_id), ObjectId(institute_id), ObjectId(school_id)


def create_faculty_document(data: Dict[str, Any]) -> Dict[str, Any]:
    user_id, institute_id, school_id = validate_faculty_data(data)
    now = datetime.now(timezone.utc)

    return {
        "user_id": user_id,
        "institute_id": institute_id,
        "school_id": school_id,

        "designation": data.get("designation"),
        "qualification": data.get("qualification"),
        "experience_years": int(data.get("experience_years", 0)),
        "specialization": data.get("specialization"),
        "bio": data.get("bio"),

        "employee_code": data.get("employee_code"),
        "joining_date": data.get("joining_date"),

        "is_active": True,

        "created_at": now,
        "updated_at": now,
    }


def update_faculty_document(data: Dict[str, Any]) -> Dict[str, Any]:
    update_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}

    allowed_fields = [
        "designation", "qualification", "experience_years", "specialization",
        "bio", "employee_code", "joining_date", "school_id", "is_active",
    ]

    for field in allowed_fields:
        if field in data:
            if field == "experience_years":
                update_fields[field] = int(data[field])
            elif field == "school_id" and ObjectId.is_valid(data[field]):
                update_fields[field] = ObjectId(data[field])
            else:
                update_fields[field] = data[field]

    return {"$set": update_fields}


def serialize_faculty(doc: Dict[str, Any], user_data: Dict[str, Any] = None) -> Dict[str, Any] | None:
    if not doc:
        return None

    return {
        "id": str(doc.get("_id")),
        "user_id": str(doc.get("user_id")),
        "institute_id": str(doc.get("institute_id")),
        "school_id": str(doc.get("school_id")),

        "fullName": user_data.get("fullName") if user_data else None,
        "email": user_data.get("email") if user_data else None,
        "phone": user_data.get("phone") if user_data else None,

        "designation": doc.get("designation"),
        "qualification": doc.get("qualification"),
        "experience_years": doc.get("experience_years"),
        "specialization": doc.get("specialization"),
        "bio": doc.get("bio"),
        "employee_code": doc.get("employee_code"),
        "joining_date": doc.get("joining_date"),

        "is_active": doc.get("is_active", True),
        "created_at": doc.get("created_at"),
    }


def delete_faculty_document(faculty_id: str) -> Dict[str, Any]:
    if not faculty_id or not ObjectId.is_valid(faculty_id):
        raise ValueError("Valid faculty_id is required")
    return {"_id": ObjectId(faculty_id)}
