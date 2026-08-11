from typing import Optional

from pydantic import BaseModel, field_validator

from app.models.faculty_material import ALLOWED_TYPES
from app.models.student_material_interaction import ALLOWED_STATUSES


class FacultyMaterialCreate(BaseModel):
    title: str
    description: Optional[str] = None
    type: str
    subject_id: str
    file_url: str
    file_id: Optional[str] = None
    filename: str
    mime_type: Optional[str] = None
    size: Optional[int] = None
    due_date: Optional[str] = None
    total_marks: Optional[float] = None
    faculty_id: Optional[str] = None  # only used/required for institute-admin callers

    @field_validator("title", "subject_id", "file_url", "filename")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be blank")
        return v

    @field_validator("type")
    @classmethod
    def valid_type(cls, v: str) -> str:
        if v not in ALLOWED_TYPES:
            raise ValueError(f"Invalid material type. Must be one of: {ALLOWED_TYPES}")
        return v


class StudentMaterialInteractionCreate(BaseModel):
    status: str = "viewed"
    submission_text: Optional[str] = None
    submission_file_url: Optional[str] = None
    submission_file_id: Optional[str] = None
    submission_filename: Optional[str] = None

    @field_validator("status")
    @classmethod
    def valid_status(cls, v: str) -> str:
        if v not in ALLOWED_STATUSES:
            raise ValueError(f"Invalid status. Must be one of: {ALLOWED_STATUSES}")
        return v
