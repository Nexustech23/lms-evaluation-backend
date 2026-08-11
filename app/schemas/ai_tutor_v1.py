from typing import Optional

from pydantic import BaseModel


class AiTutorV1UpdateRequest(BaseModel):
    notes_type: Optional[str] = None
    notes_length: Optional[str] = None
    homework_type: Optional[str] = None
    response_style: Optional[str] = None
