from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def validate_semesters(rows):
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each semester must be an object")
        value = row.get("semester_number")
        from app.models.subject import nonnegative_number
        number = nonnegative_number(value, "semester_number", integer=True)
        if number < 1 or number in seen:
            raise ValueError("Semester numbers must be positive and unique")
        seen.add(number)
        if not isinstance(row.get("subjects", []), list) or any(not isinstance(s, dict) for s in row.get("subjects", [])):
            raise ValueError("Semester subjects must be a list of objects")
    return rows

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
    _semesters = field_validator("semesters")(validate_semesters)


class UpdateBatchRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    semesters: Optional[List[Dict[str, Any]]] = None
    total_semesters: Optional[int] = Field(default=None, ge=1)
    subjectsToBeDeleted: List[str] = Field(default_factory=list)

    @field_validator("semesters")
    @classmethod
    def checked_semesters(cls, value):
        if value is None:
            raise ValueError("semesters cannot be null")
        return validate_semesters(value)


class CreateSubjectRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    school_id: str
    programme_id: str
    subject_name: str
    semester: int = Field(ge=1)
    credits: float = Field(default=0, ge=0, allow_inf_nan=False)
    teaching_periods: int = Field(default=0, ge=0)
    co_list: List[Dict[str, Any]] = Field(default_factory=list)
    co_po_matrix: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("subject_name")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("subject_name is required")
        return v


class UpdateSubjectRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    subject_name: Optional[str] = Field(default=None, min_length=1)
    credits: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    teaching_periods: Optional[int] = Field(default=None, ge=0)
    semester: Optional[int] = Field(default=None, ge=1)
    co_list: Optional[List[Dict[str, Any]]] = None
    co_po_matrix: Optional[Dict[str, Any]] = None

    @field_validator("subject_name", "credits", "teaching_periods", "semester", "co_list", "co_po_matrix")
    @classmethod
    def explicit_null_not_allowed(cls, value):
        if value is None:
            raise ValueError("Field cannot be null")
        return value
