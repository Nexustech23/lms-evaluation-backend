# ============================================================
# AI TUTOR "V1" MODEL
# Ported from models/AITutorModel.py — backs the MongoDB `ai_tutor`
# collection used by controllers/institute/ai_tutor_controller.py (the
# unused-by-frontend implementation; see app/api/routers/ai_tutor_v1.py
# for context).
# ============================================================

from datetime import datetime, timezone
from typing import Any, Dict, Optional


def create_ai_tutor_document(data: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "feature_type": data.get("feature_type", "homework"),
        "prompt": data.get("prompt", ""),
        "source_file": data.get("source_file"),
        "notes_type": data.get("notes_type"),
        "notes_length": data.get("notes_length"),
        "homework_type": data.get("homework_type"),
        "response_style": data.get("response_style"),
        "extracted_text": None,
        "generated_content": None,
        "pdf_url": None,
        "pdf_filename": None,
        "token_usage": {"gemini": None, "claude": None},
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }


def serialize_ai_tutor(document: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not document:
        return None

    def _fmt(value: Any) -> Any:
        return value.isoformat() if isinstance(value, datetime) else value

    return {
        "_id": str(document.get("_id")),
        "feature_type": document.get("feature_type"),
        "prompt": document.get("prompt"),
        "source_file": document.get("source_file"),
        "notes_type": document.get("notes_type"),
        "notes_length": document.get("notes_length"),
        "homework_type": document.get("homework_type"),
        "response_style": document.get("response_style"),
        "extracted_text": document.get("extracted_text"),
        "generated_content": document.get("generated_content"),
        "pdf_url": document.get("pdf_url"),
        "pdf_filename": document.get("pdf_filename"),
        "token_usage": document.get("token_usage"),
        "status": document.get("status"),
        "created_at": _fmt(document.get("created_at")),
        "updated_at": _fmt(document.get("updated_at")),
    }
