from pydantic import BaseModel, ConfigDict, Field, field_validator


class UploadQuestionPaperRequest(BaseModel):
    questionpaper_url: str
    fileId: str
    filename: str
    no_of_question: int

    @field_validator("questionpaper_url", "fileId", "filename")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be blank")
        return v


class RenameFolderRequest(BaseModel):
    # Wire format is the literal Mongo-style key "_id" — Pydantic v2 treats
    # leading-underscore names as private attrs, not fields, so this needs
    # an explicit alias rather than a field literally named "_id".
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(alias="_id")
    newFoldername: str

    @field_validator("newFoldername")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v:
            raise ValueError("newFoldername is required")
        return v


class DeleteFolderRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(alias="_id")


class SetArchiveStatusRequest(BaseModel):
    is_archived: bool
