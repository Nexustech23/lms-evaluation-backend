from datetime import datetime, timezone
from typing import Any, Dict, List
from bson import ObjectId
import math


def nonnegative_number(value, name, integer=False):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a finite nonnegative number")
    if isinstance(value, bool) or not math.isfinite(number) or number < 0 or (integer and not number.is_integer()):
        raise ValueError(f"{name} must be a finite nonnegative {'integer' if integer else 'number'}")
    return int(number) if integer else number


def to_object_id(value):
    if value is None:
        return None
    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str):
        value = value.strip()
        if ObjectId.is_valid(value):
            return ObjectId(value)
    raise ValueError(f"Invalid ObjectId: {value}")


def validate_co_list(co_list: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    if not isinstance(co_list, list):
        raise ValueError("co_list must be a list")

    validated_cos = []
    seen = set()
    for co in co_list:
        if not isinstance(co, dict):
            raise ValueError("Each CO must be an object")
        co_code = co.get("co_code")
        description = co.get("description")
        threshold = co.get("threshold")

        if not isinstance(co_code, str) or not co_code.strip():
            raise ValueError("Each CO must have co_code")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("Each CO must have description")
        if co_code.strip() in seen:
            raise ValueError("CO codes must be unique")
        seen.add(co_code.strip())
        if threshold is not None:
            threshold = nonnegative_number(threshold, "CO threshold")
            if threshold > 100:
                raise ValueError("CO threshold must be between 0 and 100")

        validated_cos.append({
            "co_code": co_code.strip(),
            "description": description.strip(),
            "threshold": threshold,
        })

    return validated_cos


def validate_co_po_matrix(matrix: Dict[str, Any]):
    if matrix is None:
        return None
    if not isinstance(matrix, dict):
        raise ValueError("co_po_matrix must be an object")

    validated_matrix = {}
    for co_code, po_map in matrix.items():
        if not isinstance(po_map, dict):
            raise ValueError(f"Invalid PO mapping for {co_code}")

        validated_matrix[co_code] = {}
        for po_code, level in po_map.items():
            if isinstance(level, bool) or not isinstance(level, int):
                raise ValueError(f"Invalid level for {co_code}-{po_code}")
            if level < 0 or level > 3:
                raise ValueError("Mapping level must be between 0 and 3")
            validated_matrix[co_code][po_code] = level

    return validated_matrix


def create_subject_document(data: Dict[str, Any], created_by: str) -> Dict[str, Any]:
    co_list = data.get("co_list", [])
    validated_cos = validate_co_list(co_list)

    co_po_matrix = data.get("co_po_matrix")
    validated_matrix = validate_co_po_matrix(co_po_matrix) if co_po_matrix is not None else {}
    if not isinstance(data.get("subject_name"), str) or not data["subject_name"].strip():
        raise ValueError("subject_name is required")
    semester = nonnegative_number(data.get("semester", 0), "semester", integer=True)
    if semester < 1:
        raise ValueError("semester must be at least 1")

    return {
        "institute_id": to_object_id(data["institute_id"]),
        "school_id": to_object_id(data["school_id"]),
        "programme_id": to_object_id(data["programme_id"]),
        "department_id": to_object_id(data.get("department_id")),
        "batch_id": to_object_id(data.get("batch_id")),
        "faculty_id": to_object_id(data.get("faculty_id")),

        "subject_name": data.get("subject_name"),
        "subject_code": data.get("subject_code"),

        "semester": semester,
        "credits": nonnegative_number(data.get("credits", 0), "credits"),
        "teaching_periods": nonnegative_number(data.get("teaching_periods", 0), "teaching_periods", integer=True),

        "co": validated_cos,
        "co_po_matrix": validated_matrix,

        "is_active": True,
        "is_deleted": False,

        "created_by": to_object_id(created_by),
        "created_at": datetime.now(timezone.utc),

        "updated_by": None,
        "updated_at": None,
    }


def update_subject_document(data: Dict[str, Any], updated_by: str) -> Dict[str, Any]:
    update_fields = {
        "updated_by": to_object_id(updated_by),
        "updated_at": datetime.now(timezone.utc),
    }

    allowed_fields = ["subject_name", "subject_code", "semester", "credits", "teaching_periods"]
    integer_fields = ["semester", "credits", "teaching_periods"]

    for field in allowed_fields:
        if field in data:
            update_fields[field] = int(data[field]) if field in integer_fields else data[field]

    object_id_fields = ["faculty_id", "department_id", "programme_id", "school_id", "batch_id", "institute_id"]
    for field in object_id_fields:
        if field in data and data[field]:
            update_fields[field] = to_object_id(data[field])

    if "co_list" in data:
        update_fields["co"] = validate_co_list(data["co_list"])

    if "co_po_matrix" in data:
        update_fields["co_po_matrix"] = validate_co_po_matrix(data["co_po_matrix"])

    return {"$set": update_fields}


def serialize_subject(doc: Dict[str, Any]) -> Dict[str, Any] | None:
    if not doc:
        return None

    return {
        "id": str(doc["_id"]),

        "subject_name": doc.get("subject_name"),
        "subject_code": doc.get("subject_code"),

        "semester": doc.get("semester"),
        "credits": doc.get("credits"),
        "teaching_periods": doc.get("teaching_periods"),

        "co": doc.get("co", []),
        "co_po_matrix": doc.get("co_po_matrix", {}),

        "programme_id": str(doc["programme_id"]) if doc.get("programme_id") else None,
        "school_id": str(doc["school_id"]) if doc.get("school_id") else None,
        "department_id": str(doc["department_id"]) if doc.get("department_id") else None,
        "batch_id": str(doc["batch_id"]) if doc.get("batch_id") else None,
        "faculty_id": str(doc["faculty_id"]) if doc.get("faculty_id") else None,
        "institute_id": str(doc["institute_id"]) if doc.get("institute_id") else None,

        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }
