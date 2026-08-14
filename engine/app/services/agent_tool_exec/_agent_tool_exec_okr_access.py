from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, TypedDict

from app.dao.agent_dao import agent_dao
from app.dao.user_dao import user_dao

if TYPE_CHECKING:
    from app.records.agent import AgentRecord
    from app.records.user import UserRecord


class _OKRRequestContext(TypedDict):
    agent: AgentRecord | None
    tenant_id: uuid.UUID | None
    agent_is_system: bool
    requester: UserRecord | None
    requester_user_id: uuid.UUID | None
    requester_is_admin: bool


async def _get_agent_owner_info(agent_id: uuid.UUID) -> tuple[str, str]:
    _ = await agent_dao.get(agent_id)
    return "agent", str(agent_id)


def _compute_okr_period_bounds(frequency: str, length_days: int | None):
    today = date.today()  # noqa: DTZ011 - OKR periods intentionally follow the host-local calendar.
    if frequency == "monthly":
        start = today.replace(day=1)
        if today.month == 12:
            end = today.replace(month=12, day=31)
        else:
            end = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
    elif frequency == "custom" and length_days:
        epoch = date(1970, 1, 1)
        days_since_epoch = (today - epoch).days
        period_index = days_since_epoch // length_days
        start = epoch + timedelta(days=period_index * length_days)
        end = start + timedelta(days=length_days - 1)
    else:
        quarter = (today.month - 1) // 3 + 1
        start = date(today.year, (quarter - 1) * 3 + 1, 1)
        end = date(today.year, 12, 31) if quarter == 4 else date(today.year, quarter * 3 + 1, 1) - timedelta(days=1)
    return start, end


async def _load_okr_request_context(
    db: object | None,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
) -> _OKRRequestContext:
    """Load agent + requester via DAOs. ``db`` is dual-stack compatible and ignored."""
    agent = await agent_dao.get(agent_id)
    requester = await user_dao.get(user_id) if user_id else None

    return {
        "agent": agent,
        "tenant_id": getattr(agent, "tenant_id", None),
        "agent_is_system": bool(agent and agent.is_system),
        "requester": requester,
        "requester_user_id": user_id,
        "requester_is_admin": bool(requester and requester.role in ("org_admin", "platform_admin")),
    }


def _okr_permission_denied(message: str) -> str:
    return f"Permission denied: {message}"


def _can_access_existing_okr_target(ctx: _OKRRequestContext, owner_type: str, owner_id: uuid.UUID | None) -> str | None:
    if ctx["agent_is_system"]:
        if ctx["requester_is_admin"]:
            return None
        if owner_type != "user" or owner_id != ctx["requester_user_id"]:
            return _okr_permission_denied(
                "non-admin requests may only create or modify the requester's own personal OKRs. "
                + "Do not create or edit company OKRs or other members' OKRs."
            )
        return None

    agent = ctx["agent"]
    if agent is None or owner_type != "agent" or owner_id != agent.id:
        return _okr_permission_denied("you can only create or modify your own agent OKRs.")
    return None


def _can_create_okr_target(ctx: _OKRRequestContext, owner_type: str, owner_id: uuid.UUID | None) -> str | None:
    if ctx["agent_is_system"]:
        if ctx["requester_is_admin"]:
            return None
        if owner_type != "user" or owner_id != ctx["requester_user_id"]:
            return _okr_permission_denied(
                "non-admin requests may only create the requester's own personal OKRs. "
                + "Creating company OKRs or other members' OKRs requires an org admin."
            )
        return None

    agent = ctx["agent"]
    if agent is None or owner_type != "agent" or owner_id != agent.id:
        return _okr_permission_denied("you can only create OKRs for yourself.")
    return None
