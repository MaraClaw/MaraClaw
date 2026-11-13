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


class OrganizationAdditionalAccountMonthlyUsd(IntEnum):
    """Whole USD per month for each human account beyond the included allocation, per tier; the only place the numbers live."""

    PRO = 6
    MAX = 5
    ULTRA = 4


class OrganizationMaxAccounts(IntEnum):
    """Most human accounts (organization admin included) a tier quotes, per tier; the only place the numbers live."""

    PRO = 25
    MAX = 75
    ULTRA = 200


class OrganizationAgentLimit(IntEnum):
    """Ordinary agents one account (organization admins included) may own, per tier; the only place the numbers live."""

    PRO = 2
    MAX = 5
    ULTRA = 10


class OrganizationStorageLimitBytes(IntEnum):
    """Logical bytes of current managed S3 objects one agent may hold, per tier; the only place the numbers live."""

    PRO = 1_073_741_824
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


class OrganizationPlan(BaseModel):
    """Whole-USD monthly commercial terms. The human account count includes the org admin; model charges are separate."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    id: OrganizationPlanId
    name: OrganizationPlanName
    currency: OrganizationCurrency
    monthly_price_usd: int = Field(ge=0)
    included_accounts: int = Field(ge=1)
    additional_account_monthly_usd: OrganizationAdditionalAccountMonthlyUsd
    max_accounts: OrganizationMaxAccounts
    max_agents_per_account: OrganizationAgentLimit
    storage_limit_bytes: OrganizationStorageLimitBytes
    max_file_size_bytes: OrganizationFileLimitBytes
    daily_messages_per_agent: OrganizationDailyAgentMessageLimit
