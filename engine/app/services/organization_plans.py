"""Public organization pricing catalog (whole USD per month; model charges are billed separately)."""

from uuid import UUID

from app.dao.organization_admission_dao import organization_admission_dao
from app.records.organization_plan import (
    OrganizationAdditionalAccountMonthlyUsd,
    OrganizationAgentLimit,
    OrganizationCurrency,
    OrganizationDailyAgentMessageLimit,
    OrganizationFileLimitBytes,
    OrganizationGuestCpuShares,
    OrganizationGuestMemoryBytes,
    OrganizationGuestMillicpus,
    OrganizationGuestPidsLimit,
    OrganizationMaxAccounts,
    OrganizationPlan,
    OrganizationPlanId,
    OrganizationPlanName,
    OrganizationStorageLimitBytes,
    OrganizationWorkspaceDiskBytes,
)


class AmbiguousOrganizationTierError(RuntimeError):
    """More than one approved admission claims to have provisioned the same organization."""

    def __init__(self, tenant_id: UUID):
        super().__init__(f"Organization {tenant_id} is linked to several approved admissions")


ORGANIZATION_PLANS: tuple[OrganizationPlan, ...] = (
    OrganizationPlan(
        id=OrganizationPlanId.PRO,
        name=OrganizationPlanName.PRO,
        currency=OrganizationCurrency.USD,
        monthly_price_usd=49,
        included_accounts=5,
        additional_account_monthly_usd=OrganizationAdditionalAccountMonthlyUsd.PRO,
        max_accounts=OrganizationMaxAccounts.PRO,
        max_agents_per_account=OrganizationAgentLimit.PRO,
        storage_limit_bytes=OrganizationStorageLimitBytes.PRO,
        max_file_size_bytes=OrganizationFileLimitBytes.PRO,
        daily_messages_per_agent=OrganizationDailyAgentMessageLimit.PRO,
        guest_memory_bytes=OrganizationGuestMemoryBytes.PRO,
        guest_millicpus=OrganizationGuestMillicpus.PRO,
        guest_cpu_shares=OrganizationGuestCpuShares.PRO,
        guest_pids_limit=OrganizationGuestPidsLimit.PRO,
        workspace_disk_bytes=OrganizationWorkspaceDiskBytes.PRO,
    ),
    OrganizationPlan(
        id=OrganizationPlanId.MAX,
        name=OrganizationPlanName.MAX,
        currency=OrganizationCurrency.USD,
        monthly_price_usd=149,
        included_accounts=15,
        additional_account_monthly_usd=OrganizationAdditionalAccountMonthlyUsd.MAX,
        max_accounts=OrganizationMaxAccounts.MAX,
        max_agents_per_account=OrganizationAgentLimit.MAX,
        storage_limit_bytes=OrganizationStorageLimitBytes.MAX,
        max_file_size_bytes=OrganizationFileLimitBytes.MAX,
        daily_messages_per_agent=OrganizationDailyAgentMessageLimit.MAX,
        guest_memory_bytes=OrganizationGuestMemoryBytes.MAX,
        guest_millicpus=OrganizationGuestMillicpus.MAX,
        guest_cpu_shares=OrganizationGuestCpuShares.MAX,
        guest_pids_limit=OrganizationGuestPidsLimit.MAX,
        workspace_disk_bytes=OrganizationWorkspaceDiskBytes.MAX,
    ),
    OrganizationPlan(
        id=OrganizationPlanId.ULTRA,
        name=OrganizationPlanName.ULTRA,
        currency=OrganizationCurrency.USD,
        monthly_price_usd=399,
        included_accounts=40,
        additional_account_monthly_usd=OrganizationAdditionalAccountMonthlyUsd.ULTRA,
        max_accounts=OrganizationMaxAccounts.ULTRA,
        max_agents_per_account=OrganizationAgentLimit.ULTRA,
        storage_limit_bytes=OrganizationStorageLimitBytes.ULTRA,
        max_file_size_bytes=OrganizationFileLimitBytes.ULTRA,
        daily_messages_per_agent=OrganizationDailyAgentMessageLimit.ULTRA,
        guest_memory_bytes=OrganizationGuestMemoryBytes.ULTRA,
        guest_millicpus=OrganizationGuestMillicpus.ULTRA,
        guest_cpu_shares=OrganizationGuestCpuShares.ULTRA,
        guest_pids_limit=OrganizationGuestPidsLimit.ULTRA,
        workspace_disk_bytes=OrganizationWorkspaceDiskBytes.ULTRA,
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
