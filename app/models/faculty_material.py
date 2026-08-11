from datetime import datetime, timezone
from typing import Any, Dict, Optional
from bson import ObjectId

ALLOWED_TYPES = ("Notes", "Assignments", "Class Test")


def create_faculty_material_document(data: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "title": data.get("title"),
        "description": data.get("description", ""),
        "type": data.get("type"),

        "subject_id": ObjectId(data["subject_id"]),
        "faculty_id": ObjectId(data["faculty_id"]),
        "institute_id": ObjectId(data["institute_id"]),

        "school_id": ObjectId(data["school_id"]) if data.get("school_id") else None,
        "programme_id": ObjectId(data["programme_id"]) if data.get("programme_id") else None,
        "department_id": ObjectId(data["department_id"]) if data.get("department_id") else None,
        "batch_id": ObjectId(data["batch_id"]) if data.get("batch_id") else None,

        "semester": int(data.get("semester") or 0),

        "file": {
            "url": data.get("file_url"),
            "fileId": data.get("file_id"),
            "filename": data.get("filename"),
            "mime_type": data.get("mime_type"),
            "size": data.get("size"),
        },

        "due_date": data.get("due_date"),
        "total_marks": data.get("total_marks"),

        "is_published": True,
        "is_deleted": False,

        "created_at": now,
        "updated_at": now,
    }


def serialize_faculty_material(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not doc:
        return None
    return {
        "id": str(doc["_id"]),
        "title": doc.get("title"),
        "description": doc.get("description"),
        "type": doc.get("type"),

        "subject_id": str(doc.get("subject_id")) if doc.get("subject_id") else None,
        "faculty_id": str(doc.get("faculty_id")) if doc.get("faculty_id") else None,
        "institute_id": str(doc.get("institute_id")) if doc.get("institute_id") else None,
        "school_id": str(doc.get("school_id")) if doc.get("school_id") else None,
        "programme_id": str(doc.get("programme_id")) if doc.get("programme_id") else None,
        "department_id": str(doc.get("department_id")) if doc.get("department_id") else None,
        "batch_id": str(doc.get("batch_id")) if doc.get("batch_id") else None,

        "semester": doc.get("semester"),
        "file": doc.get("file"),
        "due_date": doc.get("due_date"),
        "total_marks": doc.get("total_marks"),
        "is_published": doc.get("is_published", False),

        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }
