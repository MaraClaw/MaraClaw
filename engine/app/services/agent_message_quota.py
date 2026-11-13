"""Per-agent daily human-message cap of tier-governed organizations (``../../docs/organization-pricing.md``).

The day is the local calendar date in the timezone of the organization's genesis org admin (UTC when unset or
invalid). Usage is keyed by that date, so a new local date counts from zero without any reset job.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from fastapi import HTTPException

from app.dao.agent_daily_message_usage_dao import agent_daily_message_usage_dao
from app.dao.agent_dao import agent_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.organization_plan import OrganizationPlanId
from app.records.user import UserRecord
from app.services.admin_audit import field_change, write_admin_audit
from app.services.organization_plans import plan_for, tier_for_tenant
from app.services.quota_guard import QuotaExceeded
from app.services.schedule_time import ScheduleTimezoneError, load_timezone


@dataclass(frozen=True, slots=True)
class LocalDay:
    timezone: str
    usage_date: date
    resets_at: datetime


@dataclass(frozen=True, slots=True)
class AgentDailyMessages:
    used: int
    limit: int
    timezone: str
    resets_at: datetime


def local_day(timezone: str | None, now: datetime) -> LocalDay:
    """The calendar day ``now`` falls on in ``timezone`` and the next local midnight; unset or unknown means UTC."""
    name = timezone or "UTC"
    try:
        zone = load_timezone(name)
    except ScheduleTimezoneError:
        name, zone = "UTC", load_timezone("UTC")
    today = now.astimezone(zone).date()
    midnight = datetime.combine(today + timedelta(days=1), time.min, tzinfo=zone)
    return LocalDay(name, today, midnight.astimezone(zone))


async def _tenant_local_day(tenant_id: UUID) -> LocalDay:
    genesis = await user_dao.genesis_org_admin_for_tenant(tenant_id)
    return local_day(genesis.timezone if genesis is not None else None, datetime.now(UTC))


async def charge_agent_daily_message(agent: AgentRecord) -> None:
    """Charge one accepted human message to ``agent``; call in the accepting transaction, right after the user charge.

    System agents and organizations without a tier have no cap. The tier is read inside the caller's transaction.
    A refusal raises ``QuotaExceeded`` so the caller's rollback also undoes the user's own charge.
    """
    if agent.is_system or agent.tenant_id is None:
        return
    tier = await tier_for_tenant(agent.tenant_id)
    if tier is None:
        return
    limit = plan_for(tier).daily_messages_per_agent.value
    day = await _tenant_local_day(agent.tenant_id)
    if await agent_daily_message_usage_dao.charge(agent.id, day.usage_date, limit) is None:
        raise QuotaExceeded(
            f"This agent has reached its daily limit of {limit} messages. It resets at midnight ({day.timezone}).",
            quota_type="agent_daily_messages",
        )


async def daily_messages_for_agents(
    tenant_id: UUID | None, tier: OrganizationPlanId | None, agents: Sequence[AgentRecord]
) -> dict[UUID, AgentDailyMessages]:
    """Today's usage of each capped agent among ``agents`` (all owned inside ``tenant_id``).

    System agents and every agent of an untiered organization are absent from the result.
    """
    capped = [agent.id for agent in agents if not agent.is_system]
    if tier is None or tenant_id is None or not capped:
        return {}
    limit = plan_for(tier).daily_messages_per_agent.value
    day = await _tenant_local_day(tenant_id)
    used = await agent_daily_message_usage_dao.used_on(capped, day.usage_date)
    return {
        agent_id: AgentDailyMessages(used.get(agent_id, 0), limit, day.timezone, day.resets_at) for agent_id in capped
    }


async def reset_agent_daily_messages(
    *, actor: UserRecord, target: UserRecord, agent_id: UUID, ip_address: str | None = None
) -> AgentDailyMessages:
    """Zero today's count of an agent ``target`` created, and record the admin action in the same transaction."""
    async with connection_ctx():
        agent = await agent_dao.get(agent_id)
        if agent is None or agent.creator_id != target.id or agent.tenant_id != target.tenant_id:
            raise HTTPException(404, "Agent not found")
        tier = await tier_for_tenant(agent.tenant_id)
        if agent.is_system or tier is None or agent.tenant_id is None:
            raise HTTPException(409, "This agent has no daily message limit")
        day = await _tenant_local_day(agent.tenant_id)
        before = await agent_daily_message_usage_dao.reset(agent.id, day.usage_date)
        await write_admin_audit(
            actor=actor,
            action="agent_daily_messages_reset",
            target_type="agent",
            target_id=agent.id,
            tenant_id=agent.tenant_id,
            changes={"messages_used": field_change(before, 0)},
            details={"user_id": str(target.id), "usage_date": day.usage_date.isoformat(), "timezone": day.timezone},
            ip_address=ip_address,
        )
    return AgentDailyMessages(0, plan_for(tier).daily_messages_per_agent.value, day.timezone, day.resets_at)
