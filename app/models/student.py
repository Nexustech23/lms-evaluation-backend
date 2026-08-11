from datetime import datetime, timezone
from typing import Any, Dict
from bson import ObjectId
import re


def generate_college_email(name: str, college_short_name: str) -> str:
    """rahul + niet -> rahul.niet@gmail.com"""
    if not name:
        raise ValueError("Student name is required")

    first_name = name.strip().split(" ")[0].lower()
    first_name = re.sub(r"[^a-z0-9]", "", first_name)
    college_short_name = re.sub(r"[^a-z0-9]", "", (college_short_name or "college").lower())

    return f"{first_name}.{college_short_name}@gmail.com"


def validate_student_data(data: Dict[str, Any]):
    if not data:
        raise ValueError("No data provided")

    institute_id = data.get("institute_id")
    school_id = data.get("school_id")
    programme_id = data.get("programme_id")

    if not institute_id or not ObjectId.is_valid(institute_id):
        raise ValueError("Valid institute_id is required")

    if not school_id or not ObjectId.is_valid(school_id):
        raise ValueError("Valid school_id is required")

    if not programme_id or not ObjectId.is_valid(programme_id):
        raise ValueError("Valid programme_id is required")

    required_fields = ["name", "email", "contact_no", "enrollment_no", "roll_no"]
    for field in required_fields:
        if not data.get(field):
            raise ValueError(f"{field} is required")

    return ObjectId(institute_id), ObjectId(school_id), ObjectId(programme_id)


def create_student_document(data: Dict[str, Any]) -> Dict[str, Any]:
    institute_id, school_id, programme_id = validate_student_data(data)
    now = datetime.now(timezone.utc)

    name = data.get("name", "").strip()
    college_short_name = data.get("college_short_name", "college")
    college_email = generate_college_email(name=name, college_short_name=college_short_name)

    return {
        "user_id": data.get("user_id"),
        "institute_id": institute_id,
        "school_id": school_id,
        "programme_id": programme_id,

        "name": name,
        "father_name": data.get("father_name"),
        "dob": data.get("dob"),

        "enrollment_no": data.get("enrollment_no"),
        "roll_no": data.get("roll_no"),
        "programme_name": data.get("programme_name"),

        "email": data.get("email"),
        "college_email": college_email,
        "contact_no": data.get("contact_no"),

        "is_active": True,

        "created_at": now,
        "updated_at": now,
    }


def update_student_document(data: Dict[str, Any]) -> Dict[str, Any]:
    update_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}

    allowed_fields = [
        "name", "father_name", "dob", "email", "contact_no", "enrollment_no",
        "roll_no", "programme_name", "school_id", "programme_id", "is_active",
    ]

    for field in allowed_fields:
        if field not in data:
            continue
        value = data[field]
        if field in ["school_id", "programme_id"]:
            if ObjectId.is_valid(value):
                update_fields[field] = ObjectId(value)
        else:
            update_fields[field] = value

    if data.get("name") and data.get("college_short_name"):
        update_fields["college_email"] = generate_college_email(
            name=data["name"], college_short_name=data["college_short_name"]
        )

    return {"$set": update_fields}


def serialize_student(doc: Dict[str, Any]) -> Dict[str, Any] | None:
    if not doc:
        return None

    return {
        "id": str(doc.get("_id")),

        "institute_id": str(doc.get("institute_id")),
        "school_id": str(doc.get("school_id")),
        "programme_id": str(doc.get("programme_id")),

        "name": doc.get("name"),
        "father_name": doc.get("father_name"),
        "dob": doc.get("dob"),

        "enrollment_no": doc.get("enrollment_no"),
        "roll_no": doc.get("roll_no"),
        "programme_name": doc.get("programme_name"),

        "email": doc.get("email"),
        "college_email": doc.get("college_email"),
        "contact_no": doc.get("contact_no"),

        "is_active": doc.get("is_active", True),

        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }


def delete_student_document(student_id: str) -> Dict[str, Any]:
    if not student_id or not ObjectId.is_valid(student_id):
        raise ValueError("Valid student_id is required")
    return {"_id": ObjectId(student_id)}
