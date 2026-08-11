from datetime import datetime, timezone
from typing import Any, Dict, Optional
from bson import ObjectId


def create_student_subject_relation_document(data: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "student_id": ObjectId(data["student_id"]),
        "user_id": ObjectId(data["user_id"]),
        "institute_id": ObjectId(data["institute_id"]),
        "school_id": ObjectId(data["school_id"]),
        "programme_id": ObjectId(data["programme_id"]),
        "department_id": ObjectId(data["department_id"]) if data.get("department_id") else None,
        "batch_id": ObjectId(data["batch_id"]) if data.get("batch_id") else None,
        "subject_id": ObjectId(data["subject_id"]),

        "semester": int(data.get("semester") or 0),

        "is_active": True,
        "is_deleted": False,

        "created_at": now,
        "updated_at": now,
    }


def serialize_student_subject_relation(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not doc:
        return None
    return {
        "id": str(doc["_id"]),
        "student_id": str(doc.get("student_id")) if doc.get("student_id") else None,
        "user_id": str(doc.get("user_id")) if doc.get("user_id") else None,
        "institute_id": str(doc.get("institute_id")) if doc.get("institute_id") else None,
        "school_id": str(doc.get("school_id")) if doc.get("school_id") else None,
        "programme_id": str(doc.get("programme_id")) if doc.get("programme_id") else None,
        "department_id": str(doc.get("department_id")) if doc.get("department_id") else None,
        "batch_id": str(doc.get("batch_id")) if doc.get("batch_id") else None,
        "subject_id": str(doc.get("subject_id")) if doc.get("subject_id") else None,
        "semester": doc.get("semester"),
        "is_active": doc.get("is_active"),
        "is_deleted": doc.get("is_deleted"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }
