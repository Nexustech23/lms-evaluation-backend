from typing import List

from pydantic import BaseModel, Field


class LinkStudentSubjectsRequest(BaseModel):
    subject_ids: List[str] = Field(min_length=1)
