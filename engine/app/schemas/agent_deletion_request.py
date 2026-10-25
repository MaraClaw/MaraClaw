from datetime import datetime
from typing import Annotated, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

DeletionReason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class DeletionRequestCreate(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    agent_id: UUID
    reason: DeletionReason


class PersonRef(BaseModel):
    id: UUID
    display_name: str


class DeletionProgress(BaseModel):
    counted_approvals: int
    required_approvals: Literal[2] = 2
    genesis_approved: bool


class DeletionSummary(BaseModel):
    id: UUID
    agent_id: UUID
    agent_name: str
    status: Literal["pending", "executed", "cancelled"]
    cancel_reason: Literal["withdrawn", "owner_changed", "tenant_changed", "agent_gone"] | None
    requester: PersonRef
    created_at: datetime
    resolved_at: datetime | None
    progress: DeletionProgress


class DeletionAgent(BaseModel):
    id: UUID
    name: str
    role_description: str
    access_mode: str
    status: str
    last_active_at: datetime | None
    creator: PersonRef
    is_private: bool


class DeletionViewer(BaseModel):
    can_approve: bool
    has_viewed: bool
    has_approved: bool


class DeletionApproval(BaseModel):
    reviewer_id: UUID
    display_name: str
    is_genesis: bool
    approved_at: datetime
    counted: bool


class DeletionDetail(DeletionSummary):
    reason: str
    agent: DeletionAgent | None
    viewer: DeletionViewer
    approvals: list[DeletionApproval]


class DeletionPage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    items: list[DeletionSummary]
    next_cursor: str | None = Field(default=None)
