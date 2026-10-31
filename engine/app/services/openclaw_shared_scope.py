"""Fresh authorization and Personal route verdicts for the Shared lane, evaluated under its locks.

The obsolete receiving child's claim token and lease are deliberately not compared: a dispatch must be accepted or
refused by the CURRENT connection, owner, tenant, agent and reply route, never by who happened to receive the originals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from fastapi import HTTPException

from app.dao.chat_dao import chat_session_dao
from app.dao.whatsapp_personal_dao import get_personal
from app.records.agent import AgentRecord
from app.records.personal_burst import BurstCancelReason, PersonalBurstScope
from app.records.user import UserRecord
from app.records.whatsapp_personal import PersonalConnection, PersonalRoute, PersonalRouteError
from app.services.agent_work_eligibility import load_agent_for_new_work
from app.services.channels.whatsapp_personal_access import personal_owner
from app.services.quota_guard import AgentExpired

_OWNERSHIP_CHANGED = 409

# A verdict that is not a cancellation: the same desired connection is between sockets (process restart, transient
# reconnect), so the batch stays pending untouched and runs once that very connection is live again.
ScopeVerdict = BurstCancelReason | Literal["reconnecting"]


@dataclass(frozen=True, slots=True)
class LaneAccess:
    """Fresh eligibility of the agent (``None`` when it may not take new work) and of its Personal owner."""

    agent: AgentRecord | None
    owner: UserRecord | None
    refusal: int | None


async def authorize_lane(agent_id: UUID) -> LaneAccess:
    """Ordered tenant -> owner -> agent locks (shared, never upgraded) taken before the lane lock."""
    owner: UserRecord | None = None
    refusal: int | None = None
    try:
        _, owner = await personal_owner(agent_id, authorization_only=True)
    except HTTPException as refused:
        refusal = refused.status_code
    try:
        agent = await load_agent_for_new_work(agent_id)
    except AgentExpired, HTTPException:
        agent = None
    return LaneAccess(agent, owner, refusal)


def _route_matches(scope: PersonalBurstScope, connection: PersonalConnection) -> bool:
    return (
        connection.config_id == scope.config_id
        and connection.auth_epoch == scope.auth_epoch
        and connection.generation == scope.generation
        and connection.account_id == scope.account_id
        and scope.peer_id in {connection.pn_jid, connection.lid_jid}
    )


async def _reply_route_stale(scope: PersonalBurstScope, agent: AgentRecord) -> bool:
    reply = await chat_session_dao.get_for_agent(scope.reply_session_id, agent.id)
    if reply is None or reply.external_conv_id in (None, ""):
        return True
    try:
        route = PersonalRoute.from_external_id(reply.external_conv_id)
    except PersonalRouteError:
        return True
    return (route.config_id, route.auth_epoch, route.generation, route.account_id, route.self_jid) != (
        scope.config_id,
        scope.auth_epoch,
        scope.generation,
        scope.account_id,
        scope.peer_id,
    )


async def stale_reason(scope: PersonalBurstScope, access: LaneAccess) -> ScopeVerdict | None:
    """Why a batch may not run now: a cancel reason (originals and charges retained), ``reconnecting`` (defer), or ``None``."""
    agent, owner = access.agent, access.owner
    if access.refusal is not None or agent is None or owner is None:
        return "ownership_lost" if access.refusal == _OWNERSHIP_CHANGED else "agent_unavailable"
    if scope.agent_id != agent.id or scope.tenant_id != agent.tenant_id or scope.sender_user_id != owner.id:
        return "ownership_lost"
    connection = await get_personal(agent.id)
    if connection is None or not connection.desired:
        return "disconnected"
    if connection.owner_id != owner.id or connection.tenant_id != agent.tenant_id:
        return "ownership_lost"
    if not _route_matches(scope, connection) or await _reply_route_stale(scope, agent):
        return "route_stale"
    if connection.state == "connected":
        return None
    return "reconnecting" if connection.state == "reconnecting" else "disconnected"
