"""Platform-admin company wire models: picker options, stats rows, keyset pages and agent section visibility."""

import uuid
from datetime import date, datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.core.domain.agents import AgentStatus
from app.records.organization_plan import OrganizationPlanId
from app.schemas.agent_lifecycle import AgentLifecycleStartOut
from app.schemas.agent_runtime_loss import AgentRuntimeLossOut
from app.services.agent_work_eligibility import AgentWorkBlockCode
from app.services.tenant_lifecycle import tenant_can_be_disabled


class CompanyOption(BaseModel):
    """Stats-free company row for pickers."""

    id: uuid.UUID
    name: str
    slug: str
    is_active: bool
    sso_enabled: bool = False
    sso_domain: str | None = None
    is_system: bool = False
    is_default_end_user_org: bool = False
    created_at: datetime | None = None

    @computed_field
    @property
    def can_disable(self) -> bool:
        return tenant_can_be_disabled(self)


class CompanyStats(CompanyOption):
    user_count: int = 0
    active_user_count: int = 0
    inactive_user_count: int = 0
    agent_count: int = 0
    agent_running_count: int = 0
    total_tokens: int = 0
    cache_read_tokens_total: int = 0
    org_admin_email: str | None = None
    tier: OrganizationPlanId
    phone: str | None = None


class CompanyPageOut(BaseModel):
    items: list[CompanyStats]
    next_cursor: str | None


class CompanyOptionPageOut(BaseModel):
    """Stats-free picker page; the cursor is the one ``CompanyPageOut`` uses."""

    items: list[CompanyOption]
    next_cursor: str | None


class CompanyAgentSections(BaseModel):
    """Agent page sections: every canonical id, and the ones hidden from members of the company."""

    sections: list[str]
    hidden_sections: list[str]


class CompanyAgentSectionsUpdate(BaseModel):
    """Replacement hidden list; duplicates collapse and the stored order is canonical."""

    hidden_sections: list[str]


class CompanyRuntimeObservationOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    state: Literal["observed", "missing", "foreign", "unavailable"]
    container_status: str | None = None
    running: bool | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    restart_count_since_last_start: int | None = Field(default=None, description="Docker count since the last start.")
    oom_killed_since_last_start: bool | None = Field(default=None, description="Docker OOM flag since the last start.")


class CompanyRuntimeWorkspaceOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    used_bytes: int
    limit_bytes: int
    measured_at: datetime | None = Field(
        description="Start of the stored measurement; null while the agent was never measured (sizes are placeholders)."
    )
    over_since: datetime | None
    walk_failed_at: datetime | None = Field(
        description="Start of the latest attempt whose directory walk passed its deadline; cleared by a stored "
        "measurement that started at or after it."
    )
    remeasure_requested_at: datetime | None = Field(
        description="When a request path handed its re-measure to the quota worker; cleared by a stored measurement "
        "that started at or after it."
    )


class CompanyRuntimeAgentOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    agent_id: uuid.UUID
    name: str
    creator_id: uuid.UUID
    recorded_status: AgentStatus
    recorded_container_id: str | None
    runtime_loss: AgentRuntimeLossOut | None
    start: AgentLifecycleStartOut | None
    upgrade_in_progress: bool
    workspace: CompanyRuntimeWorkspaceOut | None
    blocking_codes: tuple[AgentWorkBlockCode, ...]
    observation: CompanyRuntimeObservationOut


class CompanyRuntimePageOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    company_id: uuid.UUID
    as_of: datetime
    items: list[CompanyRuntimeAgentOut]
    next_cursor: str | None
    observation_available: bool


class CompanyTierPreviewRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    target_tier: OrganizationPlanId


class CompanyTierChangeRequest(CompanyTierPreviewRequest):
    expected_tier: OrganizationPlanId


class CompanyTierCapacityPreview(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    used: int
    included: int
    extras: int
    limit: int
    over_included: int
    over_limit: int


class CompanyTierOwnerOverage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    user_id: uuid.UUID
    display_name: str | None
    agents_count: int
    limit: int
    overage: int


class CompanyTierRunningGuest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    agent_id: uuid.UUID
    name: str
    container_id: str


class CompanyTierRenewalPreview(BaseModel):
    """Projection of approved coverage; no existing order is repriced or created."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    billing_month: date
    extra_accounts: int
    extra_agents: int
    account_unit_usd: int
    agent_unit_usd: int
    monthly_usd: int


class CompanyTierPreview(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    current_tier: OrganizationPlanId
    target_tier: OrganizationPlanId
    accounts: CompanyTierCapacityPreview
    agents: CompanyTierCapacityPreview
    per_account_agent_limit: int | None
    owners_over_limit: list[CompanyTierOwnerOverage]
    running_guests: list[CompanyTierRunningGuest]
    guest_observation_available: bool = Field(
        description="Every retained container was observed; false includes missing or unavailable observations."
    )
    guest_limits_apply_on: Literal["stop_resume_or_replacement"] = "stop_resume_or_replacement"
    next_capacity_renewal: CompanyTierRenewalPreview
    existing_orders_keep_recorded_tier: Literal[True] = True


class CompanyTierChangeOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    previous_tier: OrganizationPlanId
    tier: OrganizationPlanId
    changed: bool
    closed_pressure_episodes: int


class CompanyActivationRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    is_active: bool


class CompanyActivationOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    ok: bool = True
    is_active: bool
    restored_members: int
    inactive_members: int
