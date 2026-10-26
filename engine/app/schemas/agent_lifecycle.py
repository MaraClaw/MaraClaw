from datetime import datetime
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.core.domain.agents import AgentStatus
from app.records.agent_setup import AgentSetupStatus
from app.records.agent_start_retry import StartFailureCode
from app.schemas.agent_setup_mcp import AgentSetupMCPStatus
from app.schemas.openclaw_updates import AgentUpdateOut
from app.services.agent_work_eligibility import AgentWorkBlockCode

type LifecycleOperationalBlockCode = Literal[
    "setup_pending", "runtime_stopped", "runtime_not_running", "upgrade_in_progress", "upgrade_required"
]
type LifecycleBlockCode = AgentWorkBlockCode | LifecycleOperationalBlockCode


class AgentLifecycleRuntimeOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    recorded_status: AgentStatus
    recorded_container_id: str | None


class AgentLifecycleStartOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    operation_id: UUID
    state: Literal["pending", "succeeded", "cancelled"]
    attempts: int
    last_attempt_at: datetime | None
    next_retry_at: datetime | None
    last_failure_code: StartFailureCode | None


class AgentLifecycleAvailabilityOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    policy_eligible: bool
    blocking_codes: tuple[LifecycleBlockCode, ...]
    can_start: bool
    can_retry_setup: bool
    can_retry_mcp: bool


class AgentLifecycleOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    as_of: datetime
    runtime: AgentLifecycleRuntimeOut
    setup: AgentSetupStatus
    start: AgentLifecycleStartOut | None
    optional_mcp: tuple[AgentSetupMCPStatus, ...]
    upgrade: AgentUpdateOut
    availability: AgentLifecycleAvailabilityOut
