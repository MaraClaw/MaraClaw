"""Usage quota guard - check and enforce usage limits."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException

from app.core.acl_locks import lock_agent, lock_tenants, lock_user
from app.core.json_types import uuid_from_row_opt
from app.dao.agent_dao import agent_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.connection import DbConnection
from app.records.agent import AgentRecord
from app.records.organization_plan import OrganizationPlanId
from app.records.user import UserRecord
from app.services.organization_plans import plan_for, tier_for_tenant


class QuotaExceededError(Exception):
    """Raised when a quota limit is reached."""

    def __init__(self, message: str, quota_type: str = "generic"):
        self.message: str = message
        self.quota_type: str = quota_type
        super().__init__(message)


class AgentExpiredError(Exception):
    """Raised when an agent has expired."""

    def __init__(self, agent_name: str = ""):
        self.message: str = f"Agent '{agent_name}' has expired and is no longer available."
        super().__init__(self.message)


QuotaExceeded = QuotaExceededError
AgentExpired = AgentExpiredError


# ── Conversation quota ──────────────────────────────────────────────


async def charge_conversation_quota(user: UserRecord) -> None:
    """Charge one accepted turn using the freshly authorized principal's exemption."""
    if user.role in ("platform_admin", "org_admin"):
        return

    if await user_dao.charge_message_quota(user.id) is None:
        current = await user_dao.get(user.id) or user
        raise QuotaExceeded(
            f"Message quota exceeded ({current.quota_messages_used}/{current.quota_message_limit}). "
            f"Period: {current.quota_message_period}.",
            quota_type="conversation",
        )


# ── Agent expiry ────────────────────────────────────────────────────


async def check_agent_expired(agent_id: uuid.UUID, *, mark: bool = True) -> None:
    """Check if agent has expired. If so, mark it and raise AgentExpired.

    ``mark=False`` only raises. Use it inside a transaction that the raise rolls back anyway: marking takes exclusive
    tenant and agent locks, which would come after the row locks that transaction already holds.
    """
    agent = await agent_dao.get(agent_id)
    if not agent:
        return

    if agent.is_expired:
        raise AgentExpired(agent.name)

    now = datetime.now(UTC)
    if agent.expires_at and now >= agent.expires_at:
        if mark:
            _ = await agent_dao.mark_expired_stopped(agent)
        raise AgentExpired(agent.name)


async def get_agent_expiry_reply(agent_name: str) -> str:
    """Return a message for when an expired agent is contacted."""
    return (
        f"I'm sorry, but I ({agent_name}) am currently unavailable. "
        "My service period has ended. Please contact the platform administrator for assistance."
    )


# ── Agent LLM call quota ───────────────────────────────────────────


async def consume_agent_llm_call(agent_id: uuid.UUID) -> None:
    if await agent_dao.consume_llm_call(agent_id) is None:
        raise QuotaExceeded(
            "Agent has reached its daily LLM call limit.",
            quota_type="agent_llm",
        )


# ── Agent creation quota ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AgentLimit:
    """Per-account ordinary-agent limit; ``limit`` is ``None`` when the account is uncapped."""

    limit: int | None
    tiered: bool


@dataclass(frozen=True, slots=True)
class AgentCapacity:
    """One account's ordinary-agent count against its limit in a single organization."""

    used: int
    limit: int
    tiered: bool

    def refuses(self, incoming: int) -> bool:
        # A tiered account already over its cap keeps its agents; only a net increase is refused.
        if self.tiered and incoming == 0:
            return False
        return self.used + incoming > self.limit


def agent_limit_for_tier(user: UserRecord, tier: OrganizationPlanId | None) -> AgentLimit:
    """An organization provisioned through an approved admission is capped by its tier for every role, replacing
    the stored per-user allowance. Any other organization keeps the stored allowance, with admins exempt."""
    if tier is not None:
        return AgentLimit(plan_for(tier).max_agents_per_account.value, tiered=True)
    if user.role in ("platform_admin", "org_admin"):
        return AgentLimit(None, tiered=False)
    return AgentLimit(user.quota_max_agents, tiered=False)


async def agent_limit(user: UserRecord, tenant_id: uuid.UUID | None) -> AgentLimit:
    """The limit governing ``user`` owning agents in ``tenant_id``; call inside the transaction holding the
    tenant/user locks so the tier is read fresh."""
    return agent_limit_for_tier(user, await tier_for_tenant(tenant_id))


async def agent_capacity(
    user: UserRecord, tenant_id: uuid.UUID | None, *, tiered_only: bool = False
) -> AgentCapacity | None:
    """Usage against ``agent_limit``; ``None`` when uncapped (or, with ``tiered_only``, not tier-governed).

    Tiered usage counts the account's non-expired, non-system agents in exactly ``tenant_id`` (any lifecycle
    status). Legacy usage keeps the creator's agents in their own current organization.
    """
    governing = await agent_limit(user, tenant_id)
    if governing.limit is None or (tiered_only and not governing.tiered):
        return None
    if governing.tiered and tenant_id is not None:
        used = await agent_dao.count_ordinary_for_creator(user.id, tenant_id)
    else:
        used = await agent_dao.count_active_for_creator(user.id)
    return AgentCapacity(used, governing.limit, governing.tiered)


async def check_agent_creation_quota(
    user: UserRecord, tenant_id: uuid.UUID | None, *, tiered_only: bool = False
) -> None:
    """Check the freshly locked principal's allowance for one more agent in the destination organization."""
    capacity = await agent_capacity(user, tenant_id, tiered_only=tiered_only)
    if capacity is not None and capacity.refuses(1):
        raise QuotaExceeded(
            f"Agent creation limit reached ({capacity.used}/{capacity.limit}).",
            quota_type="max_agents",
        )


async def check_agent_renewal_quota(db: DbConnection, agent: AgentRecord) -> None:
    """Refuse bringing an expired ordinary agent back past its owner's tier cap; call inside the writing transaction.

    Renewal is a net increase for the *owner*, whoever acts. Locks follow creation (tenants, owner, agent) and the
    decision is made on the freshly locked rows, so a racing creation, transfer or renewal cannot spend the same slot.
    Only tier-governed organizations are capped here; system agents never count."""
    owner_tenant = uuid_from_row_opt(
        await db.fetchval("SELECT tenant_id FROM users WHERE id = %(id)s", {"id": agent.creator_id})
    )
    await lock_tenants(db, (agent.tenant_id, owner_tenant))
    await lock_user(db, agent.creator_id, owner_tenant)
    await lock_agent(db, agent.id, agent.tenant_id)
    current = await agent_dao.get(agent.id)
    if current is None or current.creator_id != agent.creator_id:
        raise HTTPException(409, "Agent owner changed; retry")
    if not current.is_expired or current.is_system:
        return
    owner = await user_dao.get_with_identity(current.creator_id, fresh=True)
    if owner is None:
        raise HTTPException(404, "User not found")
    await check_agent_creation_quota(owner, current.tenant_id, tiered_only=True)


# ── Heartbeat floor enforcement ────────────────────────────────────


async def enforce_heartbeat_floor(tenant_id: uuid.UUID, floor: int | None = None) -> int:
    """Enforce heartbeat floor on all agents in the tenant.

    Args:
        tenant_id: The tenant to enforce for.
        floor: The minimum interval in minutes. If None, reads from tenant.

    Returns number of agents adjusted.
    """
    floor_val = floor
    if floor_val is None:
        tenant = await tenant_dao.get(tenant_id)
        if not tenant:
            return 0
        floor_val = tenant.min_heartbeat_interval_minutes

    return await agent_dao.raise_heartbeat_floor(tenant_id, floor_val)
