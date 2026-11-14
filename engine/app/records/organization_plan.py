"""Public organization pricing plan: wire shape and the snapshot stored with each admission request."""

from enum import IntEnum, StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class OrganizationPlanId(StrEnum):
    PRO = "pro"
    MAX = "max"
    ULTRA = "ultra"


class OrganizationPlanName(StrEnum):
    """Display names of the public organization tiers."""

    PRO = "Pro"
    MAX = "Max"
    ULTRA = "Ultra"


class OrganizationCurrency(StrEnum):
    """ISO currency codes supported by organization pricing plans."""

    USD = "USD"


class OrganizationAnnualPriceUsd(IntEnum):
    """Whole USD per year when billed annually, per tier: ten times the monthly price ("two months free"); quoted only, the engine has no purchase flow; the only place the numbers live."""

    PRO = 790
    MAX = 2490
    ULTRA = 6990


class OrganizationAdditionalAccountMonthlyUsd(IntEnum):
    """Whole USD per month for each human account beyond the included allocation, per tier; the only place the numbers live."""

    PRO = 5
    MAX = 4
    ULTRA = 3


class OrganizationAgentLimit(IntEnum):
    """Ordinary agents one account (organization admins included) may own, per tier; the only place the numbers live.

    This is the per-account limit; the company-wide agent cap is ``OrganizationIncludedAgents`` plus purchased extras.
    """

    PRO = 2
    MAX = 3
    ULTRA = 5


class OrganizationIncludedAgents(IntEnum):
    """Agents across the whole organization that the monthly price includes, per tier; the company-wide agent cap before purchased extras; the only place the numbers live."""

    PRO = 2
    MAX = 5
    ULTRA = 10


class OrganizationAdditionalAgentMonthlyUsd(IntEnum):
    """Whole USD per month for each agent beyond the included allocation, per tier; quoted only, the engine does not bill it; the only place the numbers live."""

    PRO = 35
    MAX = 49
    ULTRA = 79


class OrganizationStorageLimitBytes(IntEnum):
    """Logical bytes of current managed S3 objects one agent may hold, per tier; the only place the numbers live."""

    PRO = 2_147_483_648
    MAX = 5_368_709_120
    ULTRA = 10_737_418_240


class OrganizationFileLimitBytes(IntEnum):
    """Largest single managed file in bytes, per tier; the only place the numbers live."""

    PRO = 26_214_400
    MAX = 52_428_800
    ULTRA = 104_857_600


class OrganizationDailyAgentMessageLimit(IntEnum):
    """Human messages one agent accepts per local day (organization admin's timezone), per tier; the only place the numbers live."""

    PRO = 100
    MAX = 200
    ULTRA = 500


class OrganizationGuestMemoryBytes(IntEnum):
    """Hard memory limit, and memory+swap ceiling, of one agent's OpenClaw guest container, per tier; the only place the numbers live."""

    PRO = 2_147_483_648
    MAX = 3_221_225_472
    ULTRA = 5_368_709_120


class OrganizationGuestMillicpus(IntEnum):
    """CPU ceiling of one agent's guest in thousandths of a vCPU, per tier; the only place the numbers live."""

    PRO = 1500
    MAX = 2000
    ULTRA = 3000


class OrganizationGuestCpuShares(IntEnum):
    """Docker relative CPU weight of one agent's guest under host contention, per tier; the only place the numbers live."""

    PRO = 1024
    MAX = 1536
    ULTRA = 2048


class OrganizationGuestPidsLimit(IntEnum):
    """Most processes one agent's guest may run, per tier; the only place the numbers live."""

    PRO = 512
    MAX = 768
    ULTRA = 1024


class OrganizationWorkspaceDiskBytes(IntEnum):
    """Bytes one agent's host workspace directory plus its guest container writable layer may use before new work is refused (measured soft quota), per tier; the only place the numbers live."""

    PRO = 3_221_225_472
    MAX = 5_368_709_120
    ULTRA = 8_589_934_592


class OrganizationPlan(BaseModel):
    """Whole-USD commercial terms. The human account count includes the org admin; model charges are separate.

    ``included_accounts`` and ``included_agents`` are the company-wide caps before purchased extras (there is no
    account ceiling: a company buys any number of extra accounts), and ``max_agents_per_account`` is the per-account
    agent cap. ``annual_price_usd`` and the four ``*_addon_*`` fields are quoted terms only.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    id: OrganizationPlanId
    name: OrganizationPlanName
    currency: OrganizationCurrency
    monthly_price_usd: int = Field(ge=0)
    annual_price_usd: OrganizationAnnualPriceUsd
    included_accounts: int = Field(ge=1)
    additional_account_monthly_usd: OrganizationAdditionalAccountMonthlyUsd
    max_agents_per_account: OrganizationAgentLimit
    included_agents: OrganizationIncludedAgents
    additional_agent_monthly_usd: OrganizationAdditionalAgentMonthlyUsd
    storage_limit_bytes: OrganizationStorageLimitBytes
    max_file_size_bytes: OrganizationFileLimitBytes
    daily_messages_per_agent: OrganizationDailyAgentMessageLimit
    guest_memory_bytes: OrganizationGuestMemoryBytes
    guest_millicpus: OrganizationGuestMillicpus
    guest_cpu_shares: OrganizationGuestCpuShares
    guest_pids_limit: OrganizationGuestPidsLimit
    workspace_disk_bytes: OrganizationWorkspaceDiskBytes
    storage_addon_bytes: int = Field(ge=1)
    storage_addon_monthly_usd: int = Field(ge=1)
    workspace_disk_addon_bytes: int = Field(ge=1)
    workspace_disk_addon_monthly_usd: int = Field(ge=1)
