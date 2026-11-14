"""Public organization pricing catalog (whole USD; model charges are billed separately)."""

from uuid import UUID

from app.dao.organization_admission_dao import organization_admission_dao
from app.records.organization_plan import (
    OrganizationAdditionalAccountMonthlyUsd,
    OrganizationAdditionalAgentMonthlyUsd,
    OrganizationAgentLimit,
    OrganizationAnnualPriceUsd,
    OrganizationCurrency,
    OrganizationDailyAgentMessageLimit,
    OrganizationFileLimitBytes,
    OrganizationGuestCpuShares,
    OrganizationGuestMemoryBytes,
    OrganizationGuestMillicpus,
    OrganizationGuestPidsLimit,
    OrganizationIncludedAgents,
    OrganizationPlan,
    OrganizationPlanId,
    OrganizationPlanName,
    OrganizationStorageLimitBytes,
    OrganizationWorkspaceDiskBytes,
)

# Add-on terms are the same for every tier and quoted only (no purchase flow). Plain constants, not IntEnums: equal
# values across tiers would alias enum members.
STORAGE_ADDON_BYTES = 10_737_418_240  # 10 GiB of managed storage
STORAGE_ADDON_MONTHLY_USD = 5
WORKSPACE_DISK_ADDON_BYTES = 5_368_709_120  # 5 GiB of agent workspace disk
WORKSPACE_DISK_ADDON_MONTHLY_USD = 3


class AmbiguousOrganizationTierError(RuntimeError):
    """More than one approved admission claims to have provisioned the same organization."""

    def __init__(self, tenant_id: UUID):
        super().__init__(f"Organization {tenant_id} is linked to several approved admissions")


ORGANIZATION_PLANS: tuple[OrganizationPlan, ...] = (
    OrganizationPlan(
        id=OrganizationPlanId.PRO,
        name=OrganizationPlanName.PRO,
        currency=OrganizationCurrency.USD,
        monthly_price_usd=79,
        annual_price_usd=OrganizationAnnualPriceUsd.PRO,
        included_accounts=5,
        additional_account_monthly_usd=OrganizationAdditionalAccountMonthlyUsd.PRO,
        max_agents_per_account=OrganizationAgentLimit.PRO,
        included_agents=OrganizationIncludedAgents.PRO,
        additional_agent_monthly_usd=OrganizationAdditionalAgentMonthlyUsd.PRO,
        storage_limit_bytes=OrganizationStorageLimitBytes.PRO,
        max_file_size_bytes=OrganizationFileLimitBytes.PRO,
        daily_messages_per_agent=OrganizationDailyAgentMessageLimit.PRO,
        guest_memory_bytes=OrganizationGuestMemoryBytes.PRO,
        guest_millicpus=OrganizationGuestMillicpus.PRO,
        guest_cpu_shares=OrganizationGuestCpuShares.PRO,
        guest_pids_limit=OrganizationGuestPidsLimit.PRO,
        workspace_disk_bytes=OrganizationWorkspaceDiskBytes.PRO,
        storage_addon_bytes=STORAGE_ADDON_BYTES,
        storage_addon_monthly_usd=STORAGE_ADDON_MONTHLY_USD,
        workspace_disk_addon_bytes=WORKSPACE_DISK_ADDON_BYTES,
        workspace_disk_addon_monthly_usd=WORKSPACE_DISK_ADDON_MONTHLY_USD,
    ),
    OrganizationPlan(
        id=OrganizationPlanId.MAX,
        name=OrganizationPlanName.MAX,
        currency=OrganizationCurrency.USD,
        monthly_price_usd=249,
        annual_price_usd=OrganizationAnnualPriceUsd.MAX,
        included_accounts=15,
        additional_account_monthly_usd=OrganizationAdditionalAccountMonthlyUsd.MAX,
        max_agents_per_account=OrganizationAgentLimit.MAX,
        included_agents=OrganizationIncludedAgents.MAX,
        additional_agent_monthly_usd=OrganizationAdditionalAgentMonthlyUsd.MAX,
        storage_limit_bytes=OrganizationStorageLimitBytes.MAX,
        max_file_size_bytes=OrganizationFileLimitBytes.MAX,
        daily_messages_per_agent=OrganizationDailyAgentMessageLimit.MAX,
        guest_memory_bytes=OrganizationGuestMemoryBytes.MAX,
        guest_millicpus=OrganizationGuestMillicpus.MAX,
        guest_cpu_shares=OrganizationGuestCpuShares.MAX,
        guest_pids_limit=OrganizationGuestPidsLimit.MAX,
        workspace_disk_bytes=OrganizationWorkspaceDiskBytes.MAX,
        storage_addon_bytes=STORAGE_ADDON_BYTES,
        storage_addon_monthly_usd=STORAGE_ADDON_MONTHLY_USD,
        workspace_disk_addon_bytes=WORKSPACE_DISK_ADDON_BYTES,
        workspace_disk_addon_monthly_usd=WORKSPACE_DISK_ADDON_MONTHLY_USD,
    ),
    OrganizationPlan(
        id=OrganizationPlanId.ULTRA,
        name=OrganizationPlanName.ULTRA,
        currency=OrganizationCurrency.USD,
        monthly_price_usd=699,
        annual_price_usd=OrganizationAnnualPriceUsd.ULTRA,
        included_accounts=40,
        additional_account_monthly_usd=OrganizationAdditionalAccountMonthlyUsd.ULTRA,
        max_agents_per_account=OrganizationAgentLimit.ULTRA,
        included_agents=OrganizationIncludedAgents.ULTRA,
        additional_agent_monthly_usd=OrganizationAdditionalAgentMonthlyUsd.ULTRA,
        storage_limit_bytes=OrganizationStorageLimitBytes.ULTRA,
        max_file_size_bytes=OrganizationFileLimitBytes.ULTRA,
        daily_messages_per_agent=OrganizationDailyAgentMessageLimit.ULTRA,
        guest_memory_bytes=OrganizationGuestMemoryBytes.ULTRA,
        guest_millicpus=OrganizationGuestMillicpus.ULTRA,
        guest_cpu_shares=OrganizationGuestCpuShares.ULTRA,
        guest_pids_limit=OrganizationGuestPidsLimit.ULTRA,
        workspace_disk_bytes=OrganizationWorkspaceDiskBytes.ULTRA,
        storage_addon_bytes=STORAGE_ADDON_BYTES,
        storage_addon_monthly_usd=STORAGE_ADDON_MONTHLY_USD,
        workspace_disk_addon_bytes=WORKSPACE_DISK_ADDON_BYTES,
        workspace_disk_addon_monthly_usd=WORKSPACE_DISK_ADDON_MONTHLY_USD,
    ),
)


def plan_for(tier: OrganizationPlanId) -> OrganizationPlan:
    return next(plan for plan in ORGANIZATION_PLANS if plan.id is tier)


async def tier_for_tenant(tenant_id: UUID | None) -> OrganizationPlanId | None:
    """Tier of the approved admission that provisioned the organization, read inside the caller's transaction.

    ``None`` means the organization was not provisioned through an admission (legacy quota rules apply).
    """
    if tenant_id is None:
        return None
    tiers = await organization_admission_dao.approved_tiers_for_tenant(tenant_id)
    if len(tiers) > 1:
        raise AmbiguousOrganizationTierError(tenant_id)
    return tiers[0] if tiers else None
