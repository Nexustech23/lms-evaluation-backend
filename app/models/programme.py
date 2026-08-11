from datetime import datetime, timezone
from typing import Any, Dict, List
from bson import ObjectId


def validate_po_list(po_list: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    if not isinstance(po_list, list):
        raise ValueError("po_list must be a list")

    validated_pos = []
    for po in po_list:
        po_code = po.get("po_code")
        description = po.get("description")

        if not po_code or not isinstance(po_code, str):
            raise ValueError("Each PO must have po_code")
        if not description or not isinstance(description, str):
            raise ValueError("Each PO must have description")

        validated_pos.append({"po_code": po_code.strip(), "description": description.strip()})

    return validated_pos


def validate_targets(targets: List[Dict[str, Any]]) -> List[Dict[str, int]]:
    if not isinstance(targets, list) or len(targets) != 3:
        raise ValueError("Exactly 3 targets are required")

    validated_targets = []
    for target in targets:
        min_percentage = target.get("min_percentage")
        max_percentage = target.get("max_percentage")
        comparision_percentage = target.get("comparision_percentage")
        level = target.get("level")

        if min_percentage is None or max_percentage is None or level is None:
            raise ValueError("Each target must contain min_percentage, max_percentage and level")

        try:
            min_percentage = int(min_percentage)
            max_percentage = int(max_percentage)
            comparision_percentage = int(comparision_percentage)
            level = int(level)
        except (ValueError, TypeError):
            raise ValueError("min_percentage, max_percentage and level must be numbers")

        if not (0 <= min_percentage <= 100 and 0 <= max_percentage <= 100):
            raise ValueError("Percentages must be between 0 and 100")

        if min_percentage > max_percentage:
            raise ValueError("min_percentage cannot be greater than max_percentage")

        validated_targets.append({
            "min_percentage": min_percentage,
            "max_percentage": max_percentage,
            "comparision_percentage": comparision_percentage,
            "level": level,
        })

    return validated_targets


def validate_programme_data(data: Dict[str, Any]):
    if not data:
        raise ValueError("No data provided")

    institute_id = data.get("institute_id")
    school_id = data.get("school_id")
    programme_name = data.get("programme_name")

    if not institute_id or not ObjectId.is_valid(institute_id):
        raise ValueError("Valid institute_id is required")
    if not school_id or not ObjectId.is_valid(school_id):
        raise ValueError("Valid school_id is required")
    if not programme_name or not isinstance(programme_name, str):
        raise ValueError("programme_name is required and must be a string")

    return ObjectId(institute_id), ObjectId(school_id), programme_name.strip()


def create_programme_document(data: Dict[str, Any]) -> Dict[str, Any]:
    institute_id, school_id, programme_name = validate_programme_data(data)

    return {
        "institute_id": institute_id,
        "school_id": school_id,

        "programme_name": programme_name,
        "programme_code": data.get("programme_code"),

        "duration_years": int(data.get("duration_years") or 0),
        "total_semesters": int(data.get("total_semesters") or 0),

        "has_department": bool(data.get("has_department", False)),

        "po": [],
        "targets": [],
        "coAttainmentTarget": 0,
        "is_active": True,

        "created_at": datetime.now(timezone.utc),
        "updated_at": None,
    }


def update_programme_document(data: Dict[str, Any]) -> Dict[str, Any]:
    update_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}

    allowed_fields = [
        "programme_name", "programme_code", "duration_years",
        "total_semesters", "has_department", "is_active",
    ]

    for field in allowed_fields:
        if field in data:
            if field in ["duration_years", "total_semesters"]:
                update_fields[field] = int(data[field])
            elif field in ["has_department", "is_active"]:
                update_fields[field] = bool(data[field])
            else:
                update_fields[field] = data[field]

    return {"$set": update_fields}


def update_programme_po_targets(data: Dict[str, Any]) -> Dict[str, Any]:
    po_list = data.get("po_list")
    targets = data.get("targets")
    co_attainment_target = data.get("coAttainmentTarget")

    update_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}

    if co_attainment_target is not None:
        try:
            co_attainment_target = float(co_attainment_target)
        except (ValueError, TypeError):
            raise ValueError("coAttainmentTarget must be a valid number")

        if not (0 < co_attainment_target <= 3):
            raise ValueError("coAttainmentTarget must be greater than 0 and less than or equal to 3")

        update_fields["coAttainmentTarget"] = co_attainment_target

    if po_list is not None:
        update_fields["po"] = validate_po_list(po_list)

    if targets is not None:
        update_fields["targets"] = validate_targets(targets)

    return {"$set": update_fields}


def serialize_programme(doc: Dict[str, Any]) -> Dict[str, Any] | None:
    if not doc:
        return None

    return {
        "id": str(doc.get("_id")),

        "institute_id": str(doc.get("institute_id")) if doc.get("institute_id") else None,
        "school_id": str(doc.get("school_id")) if doc.get("school_id") else None,

        "programme_name": doc.get("programme_name"),
        "programme_code": doc.get("programme_code"),

        "duration_years": doc.get("duration_years"),
        "total_semesters": doc.get("total_semesters"),

        "has_department": doc.get("has_department", False),

        "po": doc.get("po", []),
        "targets": doc.get("targets", []),
        "coAttainmentTarget": doc.get("coAttainmentTarget"),
        "is_active": doc.get("is_active", True),

        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }
