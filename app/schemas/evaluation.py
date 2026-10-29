from typing import Any, Dict, List

from pydantic import BaseModel, Field, field_validator


class SaveEvaluationDetailsRequest(BaseModel):
    # Nested item shape stays loose (Dict) — app.models.evaluation's
    # validate_evaluation_details() already does full structural validation
    # (parameter percentage bounds, minMarks<=maxMarks, CO marks, etc.) and
    # the router calls it unchanged; this schema only guarantees the outer
    # shape (a non-empty list + a numeric totalMarks) before that runs.
    questionEvaluationDetails: List[Dict[str, Any]] = Field(min_length=1)
    totalMarks: float = Field(gt=0, allow_inf_nan=False)
    suggestion_source_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("totalMarks", mode="before")
    @classmethod
    def reject_boolean_marks(cls, value):
        if isinstance(value, bool):
            raise ValueError("totalMarks must be a number")
        return value
