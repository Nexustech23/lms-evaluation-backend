from datetime import datetime, timezone
from typing import Any, Dict
from bson import ObjectId

ALLOWED_STATUSES = ("viewed", "completed", "submitted")


def create_student_material_interaction_document(data: Dict[str, Any]) -> Dict[str, Any]:
    status = data.get("status", "viewed")
    now = datetime.now(timezone.utc)

    return {
        "material_id": ObjectId(data["material_id"]),
        "student_id": ObjectId(data["student_id"]),
        "student_user_id": ObjectId(data["student_user_id"]),
        "subject_id": ObjectId(data["subject_id"]),

        "status": status,

        "submission": {
            "text": data.get("submission_text"),
            "file_url": data.get("submission_file_url"),
            "fileId": data.get("submission_file_id"),
            "filename": data.get("submission_filename"),
        } if status == "submitted" else None,

        "marks_obtained": None,
        "feedback": None,

        "viewed_at": now if status == "viewed" else None,
        "completed_at": now if status == "completed" else None,
        "submitted_at": now if status == "submitted" else None,

        "created_at": now,
        "updated_at": now,
        "is_deleted": False,
    }
