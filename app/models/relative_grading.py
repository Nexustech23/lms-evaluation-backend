from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from bson import ObjectId

PERCENTAGE_FIELDS = [
    "a_plus_percentage", "a_percentage", "a_minus_percentage",
    "b_plus_percentage", "b_percentage", "b_minus_percentage",
    "c_plus_percentage", "c_percentage", "c_minus_percentage",
    "d_percentage", "u_percentage",
]


def validate_percentage_total(data: Dict[str, Any]) -> Optional[str]:
    try:
        total = sum(float(data.get(field, 0)) for field in PERCENTAGE_FIELDS)
    except (TypeError, ValueError):
        return "All percentage fields must be valid numbers"

    if round(total, 2) != 100:
        return f"Total percentage must equal 100, got {round(total, 2)}"

    return None


def create_relative_grading_document(data: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    doc = {"university_id": ObjectId(data["university_id"])}
    for field in PERCENTAGE_FIELDS:
        doc[field] = float(data.get(field, 0))
    doc["created_at"] = now
    doc["updated_at"] = now
    return doc


def build_relative_grading_update_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    fields = {field: float(data.get(field, 0)) for field in PERCENTAGE_FIELDS}
    fields["updated_at"] = datetime.now(timezone.utc)
    return fields


def serialize_relative_grading(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not doc:
        return None

    out = {
        "id": str(doc["_id"]),
        "university_id": str(doc["university_id"]),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }
    for field in PERCENTAGE_FIELDS:
        out[field] = doc.get(field)
    return out


def build_grading_config(grading: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Maps a saved relative-grading document to assign_relative_grades()'s input shape."""
    return [
        {"grade": "A+", "percentage": grading.get("a_plus_percentage", 0)},
        {"grade": "A", "percentage": grading.get("a_percentage", 0)},
        {"grade": "A-", "percentage": grading.get("a_minus_percentage", 0)},
        {"grade": "B+", "percentage": grading.get("b_plus_percentage", 0)},
        {"grade": "B", "percentage": grading.get("b_percentage", 0)},
        {"grade": "B-", "percentage": grading.get("b_minus_percentage", 0)},
        {"grade": "C+", "percentage": grading.get("c_plus_percentage", 0)},
        {"grade": "C", "percentage": grading.get("c_percentage", 0)},
        {"grade": "C-", "percentage": grading.get("c_minus_percentage", 0)},
        {"grade": "D", "percentage": grading.get("d_percentage", 0)},
        {"grade": "U", "percentage": grading.get("u_percentage", 0)},
    ]
