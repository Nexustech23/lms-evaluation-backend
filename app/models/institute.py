from datetime import datetime, timezone
from typing import Any, Dict
from bson import ObjectId


def validate_institute_data(data: Dict[str, Any]):
    if not data:
        raise ValueError("No data provided")

    user_id = data.get("user_id")
    institute_name = data.get("institute_name")

    if not user_id or not ObjectId.is_valid(user_id):
        raise ValueError("Valid user_id is required")

    if not institute_name or not isinstance(institute_name, str):
        raise ValueError("institute_name is required")

    return ObjectId(user_id), institute_name.strip()


def _empty_institute_token_usage() -> Dict[str, Any]:
    return {
        "gemini": {
            "total_prompt_tokens": 0,
            "total_candidate_tokens": 0,
            "total_tokens": 0,
            "call_count": 0,
        },
        "claude": {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_tokens": 0,
            "call_count": 0,
        },
        "grand_total_tokens": 0,
        "evaluation_count": 0,
    }


def create_institute_document(data: Dict[str, Any]) -> Dict[str, Any]:
    user_id, institute_name = validate_institute_data(data)
    now = datetime.now(timezone.utc)

    return {
        "user_id": user_id,

        "institute_name": institute_name,
        "short_name": data.get("short_name"),
        "institute_code": data.get("institute_code"),

        "email": data.get("email"),
        "phone": data.get("phone"),
        "website": data.get("website"),

        "address_line1": data.get("address_line1"),
        "address_line2": data.get("address_line2"),
        "city": data.get("city"),
        "state": data.get("state"),
        "country": data.get("country"),
        "pincode": data.get("pincode"),

        "affiliation": data.get("affiliation"),
        "accreditation": data.get("accreditation"),
        "established_year": data.get("established_year"),

        "logo_url": data.get("logo_url"),
        "banner_url": data.get("banner_url"),

        "description": data.get("description"),

        "is_active": True,
        "is_verified": False,

        "token_usage": _empty_institute_token_usage(),

        "created_at": now,
        "updated_at": now,
    }


def update_institute_document(data: Dict[str, Any]) -> Dict[str, Any]:
    update_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}

    allowed_fields = [
        "institute_name", "short_name", "institute_code", "email", "phone", "website",
        "address_line1", "address_line2", "city", "state", "country", "pincode",
        "affiliation", "accreditation", "established_year", "logo_url", "banner_url",
        "description", "is_active", "is_verified",
    ]

    for field in allowed_fields:
        if field in data:
            update_fields[field] = data[field]

    return {"$set": update_fields}


def _serialize_token_usage(token_usage: Dict[str, Any]) -> Dict[str, Any]:
    if not token_usage or not isinstance(token_usage, dict):
        return _empty_institute_token_usage()

    gemini = token_usage.get("gemini", {})
    claude = token_usage.get("claude", {})

    return {
        "gemini": {
            "total_prompt_tokens": gemini.get("total_prompt_tokens", 0),
            "total_candidate_tokens": gemini.get("total_candidate_tokens", 0),
            "total_tokens": gemini.get("total_tokens", 0),
            "call_count": gemini.get("call_count", 0),
        },
        "claude": {
            "total_input_tokens": claude.get("total_input_tokens", 0),
            "total_output_tokens": claude.get("total_output_tokens", 0),
            "total_tokens": claude.get("total_tokens", 0),
            "call_count": claude.get("call_count", 0),
        },
        "grand_total_tokens": token_usage.get("grand_total_tokens", 0),
        "evaluation_count": token_usage.get("evaluation_count", 0),
    }


def serialize_institute(doc: Dict[str, Any], user_data: Dict[str, Any] = None) -> Dict[str, Any] | None:
    if not doc:
        return None

    return {
        "id": str(doc.get("_id")),
        "user_id": str(doc.get("user_id")),

        "institute_name": doc.get("institute_name"),
        "short_name": doc.get("short_name"),
        "institute_code": doc.get("institute_code"),

        "email": doc.get("email"),
        "phone": doc.get("phone"),
        "website": doc.get("website"),

        "address_line1": doc.get("address_line1"),
        "address_line2": doc.get("address_line2"),
        "city": doc.get("city"),
        "state": doc.get("state"),
        "country": doc.get("country"),
        "pincode": doc.get("pincode"),

        "affiliation": doc.get("affiliation"),
        "accreditation": doc.get("accreditation"),
        "established_year": doc.get("established_year"),

        "logo_url": doc.get("logo_url"),
        "banner_url": doc.get("banner_url"),

        "description": doc.get("description"),

        "is_active": doc.get("is_active", True),
        "is_verified": doc.get("is_verified", False),

        "created_at": doc.get("created_at"),

        "admin_name": user_data.get("fullName") if user_data else None,
        "admin_email": user_data.get("email") if user_data else None,

        "hasCOAccess": user_data.get("hasCOAccess", False) if user_data else False,
        "hasQPGAccess": user_data.get("hasQPGAccess", False) if user_data else False,
        "token_usage": _serialize_token_usage(doc.get("token_usage")),
    }
