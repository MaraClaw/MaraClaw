"""Platform-admin tier previews and atomic company tier changes; guests are never mutated here."""

from datetime import timedelta
from typing import Final
from uuid import UUID

from anyio import to_thread
from fastapi import HTTPException

from app.core.acl_locks import lock_tenants
from app.core.json_types import int_from_row, str_from_row, str_from_row_opt, uuid_from_row
from app.dao.agent_resource_pressure_episode_dao import agent_resource_pressure_episode_dao as episodes
from app.dao.tenant_dao import tenant_dao
from app.db.session import connection_ctx
from app.records.organization_plan import OrganizationPlanId
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.schemas.admin_companies import (
    CompanyTierCapacityPreview,
    CompanyTierChangeOut,
    CompanyTierChangeRequest,
    CompanyTierOwnerOverage,
    CompanyTierPreview,
    CompanyTierRenewalPreview,
    CompanyTierRunningGuest,
)
from app.services.admin_audit import field_change, write_admin_audit
from app.services.agent_manager import agent_manager
from app.services.agent_resource_pressure_docker import inspect_guests
from app.services.capacity_ledger import coverage, vn_today
from app.services.capacity_pricing import month_start
from app.services.coded_errors import conflict
from app.services.company_capacity import company_caps, company_usage
from app.services.organization_plans import plan_for
from app.services.quota_guard import agent_limit_for_tier

GUEST_INSPECT_TIMEOUT_SECONDS: Final = 5.0


def _require_platform_admin(actor: UserRecord) -> None:
    if not actor.is_platform_admin:
        raise HTTPException(403, "Platform admin access required")


async def _company(tenant_id: UUID) -> TenantRecord:
    # get_many is a direct SQL read, so cached system flags cannot bypass the refusal.
    tenants = await tenant_dao.get_many([tenant_id])
    if not tenants:
        raise HTTPException(404, "Company not found")
    tenant = tenants[0]
    if tenant.is_system:
        raise conflict("system_company_tier_fixed", "System organization tiers are managed by bootstrap")
    return tenant


def _capacity(used: int, included: int, extras: int) -> CompanyTierCapacityPreview:
    return CompanyTierCapacityPreview(
        used=used,
        included=included,
        extras=extras,
        limit=included + extras,
        over_included=max(0, used - included),
        over_limit=max(0, used - included - extras),
    )


async def preview_tier_change(
    actor: UserRecord, tenant_id: UUID, target_tier: OrganizationPlanId
) -> CompanyTierPreview:
    """Advisory target policy and renewal projection, serialized with company capacity writers."""
    _require_platform_admin(actor)
    async with connection_ctx() as db:
        await lock_tenants(db, (tenant_id,), shared=True)
        tenant = await _company(tenant_id)
        caps = await company_caps(db, tenant_id, tier=target_tier)
        if caps is None:
            raise conflict("system_company_tier_fixed", "System organization tiers are managed by bootstrap")
        accounts_used, agents_used = await company_usage(db, tenant_id)
        plan = plan_for(target_tier)
        owner_limit = agent_limit_for_tier(actor, target_tier).limit
        owners = await db.fetchall(
            "SELECT a.creator_id, u.display_name, count(*) AS agents_count FROM agents a "
            "JOIN users u ON u.id = a.creator_id WHERE a.tenant_id = %(tenant)s "
            "AND a.is_expired IS FALSE AND a.is_system IS FALSE "
            "GROUP BY a.creator_id, u.display_name ORDER BY a.creator_id",
            {"tenant": tenant_id},
        )
        candidates = await db.fetchall(
            "SELECT id, name, container_id FROM agents WHERE tenant_id = %(tenant)s "
            "AND is_system IS FALSE AND container_id IS NOT NULL "
            "AND container_id <> '' ORDER BY id",
            {"tenant": tenant_id},
        )
        month = month_start(vn_today())
        covered = await coverage(db, tenant_id, month)
        account_unit = int(plan.additional_account_monthly_usd)
        agent_unit = int(plan.additional_agent_monthly_usd)
        preview = CompanyTierPreview(
            tenant_id=tenant_id,
            current_tier=tenant.tier,
            target_tier=target_tier,
            accounts=_capacity(accounts_used, caps.account_limit - caps.extras.accounts, caps.extras.accounts),
            agents=_capacity(agents_used, caps.agent_limit - caps.extras.agents, caps.extras.agents),
            per_account_agent_limit=owner_limit,
            owners_over_limit=[
                CompanyTierOwnerOverage(
                    user_id=uuid_from_row(row["creator_id"]),
                    display_name=str_from_row_opt(row["display_name"]),
                    agents_count=int_from_row(row["agents_count"]),
                    limit=owner_limit,
                    overage=int_from_row(row["agents_count"]) - owner_limit,
                )
                for row in owners
                if owner_limit is not None and int_from_row(row["agents_count"]) > owner_limit
            ],
            running_guests=[
                CompanyTierRunningGuest(
                    agent_id=uuid_from_row(row["id"]),
                    name=str_from_row(row["name"]),
                    container_id=str_from_row(row["container_id"]),
                )
                for row in candidates
            ],
            guest_observation_available=False,
            next_capacity_renewal=CompanyTierRenewalPreview(
                billing_month=month_start(month.replace(day=28) + timedelta(days=4)),
                extra_accounts=covered.accounts,
                extra_agents=covered.agents,
                account_unit_usd=account_unit,
                agent_unit_usd=agent_unit,
                monthly_usd=covered.accounts * account_unit + covered.agents * agent_unit,
            ),
        )
    references = list(dict.fromkeys(guest.container_id for guest in preview.running_guests))
    docker = agent_manager.docker_client
    inspections = (
        await to_thread.run_sync(inspect_guests, docker, references, GUEST_INSPECT_TIMEOUT_SECONDS)
        if references and docker is not None
        else None
        if references
        else {}
    )
    return preview.model_copy(
        update={
            "guest_observation_available": inspections is not None
            and all(reference in inspections for reference in references),
            "running_guests": [
                guest
                for guest in preview.running_guests
                if inspections is not None
                and (observed := inspections.get(guest.container_id)) is not None
                and observed.owner == str(guest.agent_id)
                and observed.running
            ],
        }
    )


async def apply_tier_change(
    actor: UserRecord, tenant_id: UUID, change: CompanyTierChangeRequest
) -> CompanyTierChangeOut:
    """Compare the persisted tier under the tenant lock and commit its update with episode rollover."""
    _require_platform_admin(actor)
    async with connection_ctx() as db:
        await lock_tenants(db, (tenant_id,))
        tenant = await _company(tenant_id)
        current = (await tenant_dao.tiers_for([tenant_id]))[tenant_id]
        if current != change.expected_tier:
            raise conflict("company_tier_changed", "Company tier changed; refresh the preview and try again")
        closed = 0
        changed = current != change.target_tier
        if changed:
            _ = await tenant_dao.update(db_obj=tenant, obj_in={"tier": change.target_tier})
            closed = await episodes.close_for_tenant(tenant_id, reason="tier_changed")
            await write_admin_audit(
                actor=actor,
                action="company_tier_update",
                target_type="tenant",
                target_id=tenant_id,
                tenant_id=tenant_id,
                changes={"tier": field_change(current.value, change.target_tier.value)},
            )
        return CompanyTierChangeOut(
            tenant_id=tenant_id,
            previous_tier=current,
            tier=change.target_tier,
            changed=changed,
            closed_pressure_episodes=closed,
        )
