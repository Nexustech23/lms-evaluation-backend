from typing import Optional

from pydantic import BaseModel, field_validator

from app.models.role import SYSTEM_ROLES


class RoleCreate(BaseModel):
    name: str
    description: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if v not in SYSTEM_ROLES:
            raise ValueError(f"Invalid role name. Must be one of: {list(SYSTEM_ROLES)}")
        return v
