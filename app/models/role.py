from datetime import datetime, timezone
from typing import Any, Dict

SYSTEM_ROLES = {
    "superadmin": "Platform Super Administrator",
    "institute": "Institute Administrator",
    "faculty": "Faculty Member",
}


def validate_role_data(data: Dict[str, Any]) -> str:
    if not data:
        raise ValueError("No data provided")

    name = data.get("name")
    if not name or name not in SYSTEM_ROLES:
        raise ValueError("Invalid role name")

    return name


def create_role_document(data: Dict[str, Any]) -> Dict[str, Any]:
    name = validate_role_data(data)

    return {
        "name": name,
        "display_name": SYSTEM_ROLES[name],
        "description": data.get("description"),
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
        "updated_at": None,
    }


def update_role_document(data: Dict[str, Any]) -> Dict[str, Any]:
    update_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}

    if "display_name" in data:
        update_fields["display_name"] = data["display_name"]

    if "description" in data:
        update_fields["description"] = data["description"]

    if "is_active" in data:
        update_fields["is_active"] = bool(data["is_active"])

    return {"$set": update_fields}


def serialize_role(doc: Dict[str, Any]) -> Dict[str, Any] | None:
    if not doc:
        return None

    return {
        "id": str(doc.get("_id")),
        "name": doc.get("name"),
        "display_name": doc.get("display_name"),
        "description": doc.get("description"),
        "is_active": doc.get("is_active", True),
        "created_at": doc.get("created_at"),
    }
