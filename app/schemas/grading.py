from pydantic import BaseModel, field_validator


class EvaluateAnswerScriptRequest(BaseModel):
    folderId: str
    answerId: str
    generateTranscriptPdf: bool = False

    @field_validator("folderId", "answerId")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be blank")
        return v
