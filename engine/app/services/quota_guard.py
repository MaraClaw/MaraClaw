"""Usage quota guard - check and enforce usage limits."""

import uuid
from datetime import UTC, datetime

from app.dao.agent_dao import agent_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.records.user import UserRecord


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
            + f"Period: {current.quota_message_period}.",
            quota_type="conversation",
        )


# ── Agent expiry ────────────────────────────────────────────────────


async def check_agent_expired(agent_id: uuid.UUID) -> None:
    """Check if agent has expired. If so, mark it and raise AgentExpired."""
    agent = await agent_dao.get(agent_id)
    if not agent:
        return

    if agent.is_expired:
        raise AgentExpired(agent.name)

    now = datetime.now(UTC)
    if agent.expires_at and now >= agent.expires_at:
        _ = await agent_dao.mark_expired_stopped(agent)
        raise AgentExpired(agent.name)


async def get_agent_expiry_reply(agent_name: str) -> str:
    """Return a message for when an expired agent is contacted."""
    return (
        f"I'm sorry, but I ({agent_name}) am currently unavailable. "
        + "My service period has ended. Please contact the platform administrator for assistance."
    )


# ── Agent LLM call quota ───────────────────────────────────────────


async def check_agent_llm_quota(agent_id: uuid.UUID) -> None:
    """Check if agent has remaining daily LLM calls."""
    agent = await agent_dao.get(agent_id)
    if not agent:
        return

    now = datetime.now(UTC)

    # Daily reset
    if agent.llm_calls_reset_at and now.date() > agent.llm_calls_reset_at.date():
        agent = (
            await agent_dao.update(
                db_obj=agent,
                obj_in={"llm_calls_today": 0, "llm_calls_reset_at": now},
            )
            or agent
        )

    if agent.llm_calls_today >= agent.max_llm_calls_per_day:
        raise QuotaExceeded(
            f"Agent '{agent.name}' has reached daily LLM call limit "
            + f"({agent.llm_calls_today}/{agent.max_llm_calls_per_day}).",
            quota_type="agent_llm",
        )


async def increment_agent_llm_usage(agent_id: uuid.UUID) -> None:
    """Increment agent's daily LLM call counter."""
    agent = await agent_dao.get(agent_id)
    if not agent:
        return

    now = datetime.now(UTC)
    if not agent.llm_calls_reset_at or now.date() > agent.llm_calls_reset_at.date():
        _ = await agent_dao.update(
            db_obj=agent,
            obj_in={"llm_calls_today": 1, "llm_calls_reset_at": now},
        )
    else:
        _ = await agent_dao.update(
            db_obj=agent,
            obj_in={"llm_calls_today": (agent.llm_calls_today or 0) + 1},
        )


# ── Agent creation quota ───────────────────────────────────────────


async def check_agent_creation_quota(user_id: uuid.UUID) -> None:
    """Check if user can create more agents."""
    user = await user_dao.get(user_id)
    if not user:
        return

    if user.role in ("platform_admin", "org_admin"):
        return

    current_count = await agent_dao.count_active_for_creator(user_id)

    if current_count >= user.quota_max_agents:
        raise QuotaExceeded(
            f"Agent creation limit reached ({current_count}/{user.quota_max_agents}).",
            quota_type="max_agents",
        )


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
