from datetime import datetime, timezone
from typing import Any, Dict
from bson import ObjectId


def validate_school_data(data: Dict[str, Any]):
    if not data:
        raise ValueError("No data provided")

    institute_id = data.get("institute_id")
    school_name = data.get("school_name")

    if not institute_id or not ObjectId.is_valid(institute_id):
        raise ValueError("Valid institute_id is required")

    if not school_name or not isinstance(school_name, str):
        raise ValueError("school_name is required and must be a string")

    school_code = data.get("school_code")
    description = data.get("description")
    image_url = data.get("image_url")
    established_year = data.get("established_year")

    if established_year:
        try:
            established_year = int(established_year)
        except ValueError:
            raise ValueError("established_year must be a number")

    return institute_id, school_name.strip(), school_code, description, image_url, established_year


def create_school_document(data: Dict[str, Any], created_by: str) -> Dict[str, Any]:
    (institute_id, school_name, school_code, description, image_url,
     established_year) = validate_school_data(data)
    now = datetime.now(timezone.utc)

    return {
        "institute_id": ObjectId(institute_id),

        "school_name": school_name,
        "school_code": school_code.strip() if school_code else None,
        "description": description.strip() if description else None,
        "image_url": image_url.strip() if image_url else None,
        "established_year": established_year,

        "is_active": True,
        "is_deleted": False,

        "created_by": ObjectId(created_by) if ObjectId.is_valid(created_by) else created_by,
        "updated_by": None,

        "created_at": now,
        "updated_at": now,
    }


def update_school_document(data: Dict[str, Any], updated_by: str) -> Dict[str, Any]:
    update_fields: Dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc),
        "updated_by": ObjectId(updated_by) if ObjectId.is_valid(updated_by) else updated_by,
    }

    allowed_fields = [
        "school_name", "school_code", "description", "image_url",
        "established_year", "is_active", "is_deleted",
    ]

    for field in allowed_fields:
        if field in data:
            if field == "established_year" and data[field]:
                try:
                    update_fields[field] = int(data[field])
                except ValueError:
                    raise ValueError("established_year must be a number")
            else:
                update_fields[field] = data[field]

    return {"$set": update_fields}


def serialize_school(doc: Dict[str, Any]) -> Dict[str, Any] | None:
    if not doc:
        return None

    return {
        "id": str(doc.get("_id")),
        "institute_id": str(doc.get("institute_id")) if doc.get("institute_id") else None,

        "school_name": doc.get("school_name"),
        "school_code": doc.get("school_code"),
        "description": doc.get("description"),
        "image_url": doc.get("image_url"),
        "established_year": doc.get("established_year"),

        "is_active": doc.get("is_active", True),
        "is_deleted": doc.get("is_deleted", False),

        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }
