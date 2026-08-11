from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict


class AiDrivenGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt: str
    title: Optional[str] = None
    total_study_time: int = 60
    revision_time: int = 10
    test_duration: int = 5
    test_format: str = "mcq"
    num_tests: int = 3


class CustomCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: Optional[str] = None
    study_time_mins: int = 25
    break_time_mins: int = 5
    num_sessions: int = 4


class SubmitTestRequest(BaseModel):
    section_index: int = 0
    # Answer objects stay loose — grading reads fields defensively (.get())
    # and each answer's shape already varies (typed text vs. base64 image
    # data for OCR'd handwritten answers).
    answers: List[Dict[str, Any]] = []


class CompleteSessionRequest(BaseModel):
    # Original behavior silently coerced any unrecognized status to
    # "completed" rather than rejecting it; Pydantic now rejects it (422) —
    # an intentional tightening, consistent with this retrofit's treatment
    # of other silent-fallback-on-bad-input cases (e.g. ai-assisted/upload's
    # Form int coercion).
    status: Literal["completed", "interrupted"] = "completed"
    total_focused_mins: int = 0
