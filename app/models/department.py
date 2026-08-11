from datetime import datetime, timezone
from typing import Any, Dict
from bson import ObjectId


def validate_department_data(data: Dict[str, Any]):
    if not data:
        raise ValueError("No data provided")

    institute_id = data.get("institute_id")
    programme_id = data.get("programme_id")
    school_id = data.get("school_id")
    department_name = data.get("department_name")

    if not institute_id or not ObjectId.is_valid(institute_id):
        raise ValueError("Valid institute_id is required")
    if not school_id or not ObjectId.is_valid(school_id):
        raise ValueError("Valid school_id is required")
    if not programme_id or not ObjectId.is_valid(programme_id):
        raise ValueError("Valid programme_id is required")
    if not department_name or not isinstance(department_name, str):
        raise ValueError("department_name is required and must be a string")

    return ObjectId(institute_id), ObjectId(school_id), ObjectId(programme_id), department_name.strip()


def create_department_document(data: Dict[str, Any]) -> Dict[str, Any]:
    institute_id, school_id, programme_id, department_name = validate_department_data(data)
    hod_user_id = data.get("hod_user_id")

    return {
        "institute_id": institute_id,
        "programme_id": programme_id,
        "school_id": school_id,
        "department_name": department_name,

        "code": data.get("code"),
        "hod_user_id": ObjectId(hod_user_id) if hod_user_id and ObjectId.is_valid(hod_user_id) else None,

        "is_active": True,
        "created_at": datetime.now(timezone.utc),
        "updated_at": None,
    }


def update_department_document(data: Dict[str, Any]) -> Dict[str, Any]:
    update_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}

    if "department_name" in data:
        if not data["department_name"]:
            raise ValueError("department_name cannot be empty")
        update_fields["department_name"] = data["department_name"].strip()

    if "code" in data:
        update_fields["code"] = data["code"]

    if "hod_user_id" in data:
        update_fields["hod_user_id"] = data["hod_user_id"]

    if "is_active" in data:
        update_fields["is_active"] = bool(data["is_active"])

    return {"$set": update_fields}


def serialize_department(doc: Dict[str, Any]) -> Dict[str, Any] | None:
    if not doc:
        return None

    return {
        "id": str(doc.get("_id")),
        "institute_id": str(doc.get("institute_id")) if doc.get("institute_id") else None,
        "school_id": str(doc.get("school_id")) if doc.get("school_id") else None,
        "programme_id": str(doc.get("programme_id")) if doc.get("programme_id") else None,
        "department_name": doc.get("department_name"),
        "code": doc.get("code"),
        "hod_user_id": doc.get("hod_user_id"),
        "is_active": doc.get("is_active", True),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }
