from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator
from app.models.exam_validation import finite_number


class UploadAnswerScriptRequest(BaseModel):
    answer_script_url: str
    fileId: str
    filename: str

    @field_validator("answer_script_url", "fileId", "filename")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be blank")
        return v


class RenameFileRequest(BaseModel):
    answer_id: str
    newFilename: str


class DeleteFileRequest(BaseModel):
    answer_id: str


class SelfEvaluationRequest(BaseModel):
    answer_id: str
    # Review validation runs after ownership checks, against saved question
    # limits. Keep the flexible AI evaluation shape at the transport boundary.
    questionwise_marking: List[Dict[str, Any]] = []


class ManualMarksEntryRequest(BaseModel):
    # Only required when the exam has no saved rubric (flat-total fallback);
    # when a rubric exists, total max marks comes from it instead.
    max_marks: Optional[float] = Field(default=None, gt=0)

    @field_validator("max_marks", mode="before")
    @classmethod
    def finite_maximum(cls, value):
        return None if value is None else finite_number(value, "max_marks")
    # The complete batch is validated before any write, so invalid rows
    # cannot be silently skipped while the UI reports the whole batch saved.
    # Rubric path: {student_id, question_marks: [{question_no, marks}]}
    # Fallback path: {student_id, marks}
    entries: List[Dict[str, Any]] = []
