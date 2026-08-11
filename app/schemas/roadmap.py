from typing import Any, Dict, Optional

from pydantic import BaseModel, field_validator


class PreAssessmentRequest(BaseModel):
    subject: str

    @field_validator("subject")
    @classmethod
    def valid_subject(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("subject is required")
        if len(v) > 200:
            raise ValueError("subject must be 200 characters or fewer")
        return v


class CreateRoadmapRequest(BaseModel):
    subject: str
    goal: Optional[str] = ""
    skill_level: str = "Beginner"
    daily_study_time: str = "1 Hour"
    revision_frequency: str = "Every Week"
    assessment_score: Optional[Any] = None

    @field_validator("subject")
    @classmethod
    def valid_subject(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("subject is required")
        if len(v) > 200:
            raise ValueError("subject must be 200 characters or fewer")
        return v

    @field_validator("goal")
    @classmethod
    def valid_goal(cls, v: Optional[str]) -> str:
        v = (v or "").strip()
        if len(v) > 500:
            raise ValueError("goal must be 500 characters or fewer")
        return v


class UpdateSubtopicRequest(BaseModel):
    subtopic_key: str
    completed: bool = True

    @field_validator("subtopic_key")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v:
            raise ValueError("subtopic_key is required")
        return v


class SubmitQuizRequest(BaseModel):
    level: int = 1
    answers: Dict[str, Any] = {}
