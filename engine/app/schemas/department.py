"""Department administration HTTP boundaries."""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, ClassVar
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


class DepartmentDeletionPreviewOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)
    membership_count: int = Field(
        ge=0, description="Department membership associations currently selected for removal."
    )
    agent_grant_count: int = Field(
        ge=0, description="Department-to-agent grant associations, not people losing effective access."
    )


type DepartmentMergeConfirmation = Annotated[str, Field(pattern=r"^v1:[0-9a-f]{64}$", strict=True)]


class DepartmentMergeRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    destination_id: UUID
    confirmation_token: DepartmentMergeConfirmation


@dataclass(frozen=True, slots=True)
class DepartmentMergeSelection:
    source_id: UUID
    destination_id: UUID
    tenant_id: UUID | None


class DepartmentMergePreviewOut(BaseModel):
    """Reviewed associations whose confirmation is checked by apply under its locks."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)
    source: DepartmentOut
    destination: DepartmentOut
    confirmation_token: DepartmentMergeConfirmation
    source_membership_count: int = Field(ge=0)
    destination_membership_count: int = Field(ge=0)
    overlapping_membership_count: int = Field(ge=0)
    source_agent_grant_count: int = Field(ge=0, description="Eligible custom-agent department use grants.")
    destination_agent_grant_count: int = Field(ge=0, description="Eligible custom-agent department use grants.")
    overlapping_agent_grant_count: int = Field(ge=0)


class DepartmentOptionOut(BaseModel):
    id: UUID
    name: str


class DepartmentOptionsOut(BaseModel):
    departments: list[DepartmentOptionOut]


class DepartmentMemberOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)
    membership_id: UUID
    display_name: str


class DepartmentMembersOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)
    members: list[DepartmentMemberOut]


class DepartmentAssignment(BaseModel):
    department_id: UUID | None


class AssignmentOut(DepartmentAssignment):
    user_id: UUID
    tenant_id: UUID


class DepartmentAssignments(BaseModel):
    department_ids: list[UUID]

    @field_validator("department_ids")
    @classmethod
    def normalize_departments(cls, value: list[UUID]) -> list[UUID]:
        return sorted(set(value))


class AssignmentsOut(DepartmentAssignments):
    user_id: UUID
    tenant_id: UUID


class DepartmentsOut(BaseModel):
    departments: list[DepartmentOut]
    assignments: list[AssignmentOut]
