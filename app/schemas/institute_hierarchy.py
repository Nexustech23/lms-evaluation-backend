from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, field_validator

# Every create/update pair below delegates real field-level validation to
# app.models.{school,programme,department,batch,subject}'s existing
# validate_*_data()/create_*_document()/update_*_document() functions,
# which already raise ValueError -> 400 with specific messages the router
# forwards unchanged. These schemas only add: (a) a 422 instead of a 500/
# KeyError-shaped failure when the body isn't even a JSON object, and (b)
# typed validation for the one field each create endpoint can't function
# without. extra="allow" preserves every optional passthrough field
# (description, image_url, established_year, po/targets lists, etc.)
# unchanged, since the router mutates the dict (adding institute_id/
# school_id/programme_id resolved server-side) before handing it to the
# model layer either way.


class CreateSchoolRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    school_name: str

    @field_validator("school_name")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("school_name is required")
        return v


class UpdateSchoolRequest(BaseModel):
    model_config = ConfigDict(extra="allow")


class CreateProgrammeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    school_id: str
    programme_name: str

    @field_validator("programme_name")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("programme_name is required")
        return v


class UpdateProgrammeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")


class UpdateProgrammePoRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    programme_id: str


class CreateDepartmentRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    programme_id: str
    department_name: str

    @field_validator("department_name")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("department_name is required")
        return v


class UpdateDepartmentRequest(BaseModel):
    model_config = ConfigDict(extra="allow")


class CreateBatchRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    programme_id: Optional[str] = None
    department_id: Optional[str] = None
    # Nested semester/subject items keep their existing free-form shape —
    # already validated item-by-item in app.models.batch.create_batch_document
    # and, for the nested subject-creation loop, app.models.subject's own
    # create_subject_document per item.
    semesters: List[Dict[str, Any]]


class UpdateBatchRequest(BaseModel):
    model_config = ConfigDict(extra="allow")


class CreateSubjectRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    school_id: str
    programme_id: str
    subject_name: str

    @field_validator("subject_name")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("subject_name is required")
        return v


class UpdateSubjectRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
