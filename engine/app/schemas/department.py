"""Department administration HTTP boundaries."""

from datetime import datetime
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DepartmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class DepartmentOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    name: str
    created_at: datetime


class DepartmentAssignment(BaseModel):
    department_id: UUID | None


class AssignmentOut(DepartmentAssignment):
    user_id: UUID
    tenant_id: UUID


class DepartmentsOut(BaseModel):
    departments: list[DepartmentOut]
    assignments: list[AssignmentOut]
