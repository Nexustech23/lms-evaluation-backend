from datetime import datetime, timezone
from typing import Any, Dict
import re

ALLOWED_CONTACT_ROLES = {"institute", "administrator", "ai-tutor", "tutor"}
EMAIL_REGEX = r"^[\w\.-]+@[\w\.-]+\.\w+$"


def _validate_email(email: str) -> str:
    if not email or not re.match(EMAIL_REGEX, email):
        raise ValueError("Valid email is required")
    return email.lower().strip()


def _validate_role(role: str) -> str:
    if not role:
        raise ValueError("Role is required")
    role = role.strip().lower()
    if role not in ALLOWED_CONTACT_ROLES:
        raise ValueError("Invalid role")
    return role


def create_contact_document(data: Dict[str, Any]) -> Dict[str, Any]:
    first_name = data.get("first_name")
    last_name = data.get("last_name")
    topic = data.get("topic")
    message = data.get("message")

    if not first_name:
        raise ValueError("First name is required")
    if not last_name:
        raise ValueError("Last name is required")
    if not topic:
        raise ValueError("Topic is required")
    if not message:
        raise ValueError("Message is required")

    now = datetime.now(timezone.utc)

    return {
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "role": _validate_role(data.get("role")),
        "topic": topic.strip(),
        "email": _validate_email(data.get("email")),
        "contact_no": data.get("contact_no"),
        "message": message.strip(),
        "read": bool(data.get("read", False)),
        "created_at": now,
        "updated_at": now,
    }


def serialize_contact(doc: Dict[str, Any]) -> Dict[str, Any] | None:
    if not doc:
        return None

    return {
        "id": str(doc["_id"]),
        "first_name": doc.get("first_name"),
        "last_name": doc.get("last_name"),
        "role": doc.get("role"),
        "topic": doc.get("topic"),
        "email": doc.get("email"),
        "contact_no": doc.get("contact_no"),
        "message": doc.get("message"),
        "read": bool(doc.get("read", False)),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }
