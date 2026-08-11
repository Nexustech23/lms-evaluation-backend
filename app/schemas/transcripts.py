from pydantic import BaseModel


class TranscriptGenerateRequest(BaseModel):
    batch_id: str
    semester: int
