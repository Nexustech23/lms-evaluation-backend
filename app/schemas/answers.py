from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


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
    # Individual question-marking dicts stay loose — this mirrors the AI
    # evaluation pipeline's own free-form questionwise_marking shape, which
    # is read defensively (.get(...)) both here and in subject_results.py.
    questionwise_marking: List[Dict[str, Any]] = []


class ManualMarksEntryRequest(BaseModel):
    # Only required when the exam has no saved rubric (flat-total fallback);
    # when a rubric exists, total max marks comes from it instead.
    max_marks: Optional[float] = Field(default=None, gt=0)
    # Each entry is validated and individually skipped on bad data by the
    # router (missing student_id, out-of-range marks) rather than rejecting
    # the whole batch — kept as loose dicts to preserve that per-row
    # tolerance, matching bulk-student-enrollment's equivalent design.
    # Rubric path: {student_id, question_marks: [{question_no, marks}]}
    # Fallback path: {student_id, marks}
    entries: List[Dict[str, Any]] = []
