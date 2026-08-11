# ============================================================
# ROADMAP MODEL
# Ported from models/RoadmapModel.py.
# ============================================================

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bson import ObjectId


def create_roadmap_document(user_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "user_id": ObjectId(user_id),
        "subject": data.get("subject", "General Subject"),
        "goal": data.get("goal", "Skill Upgrade"),
        "skill_level": data.get("skill_level", "Beginner"),
        "daily_study_time": data.get("daily_study_time", "1 Hour"),
        "revision_frequency": data.get("revision_frequency", "Every Week"),
        "assessment_score": data.get("assessment_score"),
        "stats": data.get("stats", {}),
        "levels": data.get("levels", []),
        "progress": {
            "overallProgress": 0,
            "completedSubtopics": [],
            "passedQuizzes": {},
            "quizHistory": [],
            "weakTopics": [],
            "streakDays": 0,
        },
        "unlockedLevels": data.get("unlockedLevels", [1]),
        "active": True,
        "created_at": now,
        "updated_at": now,
    }


def serialize_roadmap(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
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
                serialize_roadmap(item) if isinstance(item, dict)
                else str(item) if isinstance(item, ObjectId)
                else item
                for item in value
            ]
        elif isinstance(value, dict):
            result[key] = serialize_roadmap(value)
        else:
            result[key] = value

    return result
