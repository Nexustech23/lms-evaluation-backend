import re
from typing import Optional

from pydantic import BaseModel, field_validator

from app.models.contact import ALLOWED_CONTACT_ROLES, EMAIL_REGEX


class ContactCreate(BaseModel):
    first_name: str
    last_name: str
    role: str
    topic: str
    email: str
    message: str
    contact_no: Optional[str] = None

    @field_validator("first_name", "last_name", "topic", "message")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be blank")
        return v

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in ALLOWED_CONTACT_ROLES:
            raise ValueError(f"Invalid role. Must be one of: {sorted(ALLOWED_CONTACT_ROLES)}")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if not v or not re.match(EMAIL_REGEX, v):
            raise ValueError("Valid email is required")
        return v.lower().strip()
