# ============================================================
# POMODORO SESSION MODEL
# Ported from models/PomodoroModel.py.
#
# NOTE: VALID_MODES / VALID_FORMATS / VALID_STATUS mirror the Flask
# constants but, like the Flask original, are not enforced anywhere —
# kept for parity/reference only, not imported by the router.
# ============================================================

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bson import ObjectId

VALID_MODES = {"ai-driven", "ai-assisted", "custom"}
VALID_FORMATS = {"mcq", "written", "mixed"}
VALID_STATUS = {"active", "completed", "interrupted"}


def create_ai_driven_document(user_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "user_id": ObjectId(user_id),
        "mode": "ai-driven",
        "title": data.get("title", "AI-Driven Session"),
        "status": "active",
        "prompt": data.get("prompt", ""),
        "total_study_time_mins": int(data.get("total_study_time", 60)),
        "revision_time_mins": int(data.get("revision_time", 10)),
        "test_duration_mins": int(data.get("test_duration", 5)),
        "test_format": data.get("test_format", "mcq"),
        "num_tests": int(data.get("num_tests", 3)),
        "sections": [],
        "current_section_index": 0,
        "total_focused_mins": 0,
        "evaluation": None,
        "started_at": None,
        "completed_at": None,
        "created_at": now,
        "updated_at": now,
    }


def create_ai_assisted_document(user_id: str, data: Dict[str, Any], file_url: str, file_name: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "user_id": ObjectId(user_id),
        "mode": "ai-assisted",
        "title": data.get("title", "AI-Assisted Session"),
        "status": "active",
        "uploaded_file_url": file_url,
        "uploaded_file_name": file_name,
        "total_study_time_mins": int(data.get("total_study_time", 60)),
        "revision_time_mins": int(data.get("revision_time", 10)),
        "test_duration_mins": int(data.get("test_duration", 5)),
        "test_format": data.get("test_format", "mcq"),
        "num_tests": int(data.get("num_tests", 3)),
        "sections": [],
        "current_section_index": 0,
        "total_focused_mins": 0,
        "evaluation": None,
        "started_at": None,
        "completed_at": None,
        "created_at": now,
        "updated_at": now,
    }


def create_custom_document(user_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "user_id": ObjectId(user_id),
        "mode": "custom",
        "title": data.get("title", "Custom Focus Session"),
        "status": "active",
        "study_time_mins": int(data.get("study_time_mins", 25)),
        "break_time_mins": int(data.get("break_time_mins", 5)),
        "num_sessions": int(data.get("num_sessions", 4)),
        "sections": [],
        "total_focused_mins": 0,
        "evaluation": None,
        "started_at": None,
        "completed_at": None,
        "created_at": now,
        "updated_at": now,
    }


def serialize_session(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not doc:
        return None

    result: Dict[str, Any] = {}
    for key, value in doc.items():
        if isinstance(value, ObjectId):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        elif isinstance(value, list):
            result[key] = [
                serialize_session(item) if isinstance(item, dict)
                else str(item) if isinstance(item, ObjectId)
                else item
                for item in value
            ]
        elif isinstance(value, dict):
            result[key] = serialize_session(value)
        else:
            result[key] = value

    return result
