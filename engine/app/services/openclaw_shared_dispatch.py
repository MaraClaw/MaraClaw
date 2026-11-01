"""The one Shared-lane claim: immediate wake and guest poll both come through ``claim_ready_shared``.

Order, never reversed: guest-write flock -> one short database transaction { tenant/owner/agent shared locks ->
shared chat-session lock -> the ACTUAL oldest pending head -> (Personal batch: fresh scope, seal, route) -> guest
configuration -> body/routing/delivered + batch claimed } -> COMMIT. Dispatch to the guest runs after the commit,
holding no lock. An unready head blocks every younger row; an active (delivered, even uncertain) turn blocks the lane.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import override
from uuid import UUID

from app.core.logging import logger
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.personal_burst_dispatch_dao import personal_burst_dispatch_dao
from app.db.session import connection_ctx, get_connection
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.openclaw_dispatch import (
    BURST_ROUTING_REASON,
    SharedBlock,
    SharedClaim,
    SharedClaimResult,
    SharedDispatchRequest,
)
from app.records.personal_burst import PersonalBurstBatch, PersonalBurstHead
from app.services.agent_setup_lock import WorkspaceLockTimeoutError
from app.services.openclaw_guest_config import GuestConfigWriteError, apply_guest_model
from app.services.openclaw_model_selection import (
    NoCompanyModelError,
    ResolvedModel,
    choose_route,
    resolve_stored_model,
)
from app.services.openclaw_shared_scope import LaneAccess, authorize_lane, stale_reason
from app.services.openclaw_update_exclusion import guest_write_section
from app.services.shared_chat import shared_session_id

_MAX_PASSES = 16
_CANCELLED = SharedClaimResult(None, "cancelled")


@dataclass(slots=True)
class SharedDispatchTransactionError(RuntimeError):
    """The central claim owns its transaction: it must not run inside a caller's."""

    agent_id: UUID

    @override
    def __str__(self) -> str:
        return f"Shared dispatch for agent {self.agent_id} must not run inside a caller's transaction"


@dataclass(slots=True)
class SharedBatchStateError(RuntimeError):
    """A sealed batch could not be marked claimed in the transaction that delivered its head."""

    gateway_id: UUID

    @override
    def __str__(self) -> str:
        return f"Personal burst {self.gateway_id} could not be marked claimed with its delivery"


def _blocked(reason: SharedBlock) -> SharedClaimResult:
    return SharedClaimResult(None, reason)


async def claim_ready_shared(request: SharedDispatchRequest) -> GatewayMessageRecord | None:
    """The claimed, committed head of the agent's Shared lane, or ``None`` (see ``claim_shared_turn``)."""
    return (await claim_shared_turn(request)).message


async def claim_shared_turn(request: SharedDispatchRequest) -> SharedClaimResult:
    """Claim the actual oldest Shared head iff it is ready, in scope and routable; commit before returning."""
    if get_connection() is not None:
        raise SharedDispatchTransactionError(request.agent_id)
    try:
        async with guest_write_section(request.agent_id), connection_ctx():
            return await _claim_in_transaction(request)
    except WorkspaceLockTimeoutError:
        logger.info("[OpenClaw] shared claim left pending for {}: guest write lock busy", request.agent_id)
        return _blocked("lock_timeout")


async def _claim_in_transaction(request: SharedDispatchRequest) -> SharedClaimResult:
    agent_id, lane = request.agent_id, shared_session_id(request.agent_id)
    # Unlocked fast exit: a stale read can only miss a claim (the next wake/poll retries), never make a wrong one.
    peek = await personal_burst_dispatch_dao.head(agent_id, lane)
    if peek is None:
        return _blocked("empty")
    if peek.active:
        return _blocked("active")
    if peek.batch is not None and peek.batch.state == "pending" and not peek.due:
        return _blocked("not_due")
    access = await authorize_lane(agent_id)
    if not await gateway_message_dao.lock_shared_lane(agent_id, lane):
        return _blocked("lane_missing")
    for _ in range(_MAX_PASSES):
        step = await _claim_head(request, access, lane)
        if step is not _CANCELLED:
            return step
    return _CANCELLED


async def _claim_head(request: SharedDispatchRequest, access: LaneAccess, lane: UUID) -> SharedClaimResult:
    head = await personal_burst_dispatch_dao.head(request.agent_id, lane)
    if head is None:
        return _blocked("empty")
    if head.active:
        return _blocked("active")
    message = await gateway_message_dao.get_for_agent(head.gateway_id, request.agent_id)
    if message is None:
        return _blocked("lost")
    if head.batch is not None:
        return await _claim_batch(request, access, head, head.batch, message)
    if message.routing_reason == BURST_ROUTING_REASON:
        return await _drop_headless(message)
    return await _claim_stored(request, access, message)


async def _drop_headless(message: GatewayMessageRecord) -> SharedClaimResult:
    """A burst marker without its header (or with a terminal one) is never a legacy ready row: fail closed."""
    _ = await gateway_message_dao.cancel_anchor(message.id, message.agent_id)
    logger.warning("[OpenClaw] shared anchor {} cancelled: Personal burst header unavailable", message.id)
    return _CANCELLED


async def _claim_batch(
    request: SharedDispatchRequest,
    access: LaneAccess,
    head: PersonalBurstHead,
    batch: PersonalBurstBatch,
    message: GatewayMessageRecord,
) -> SharedClaimResult:
    if batch.state in ("completed", "cancelled"):
        return await _drop_headless(message)
    if batch.state == "pending" and not head.due:
        return _blocked("not_due")
    if access.agent is None and access.refusal is None:
        return _blocked("agent_unavailable")
    reason = await stale_reason(batch.scope, access)
    if reason == "reconnecting":
        return _blocked("reconnecting")
    if reason is not None:
        if batch.state == "claimed":
            _ = await gateway_message_dao.cancel_anchor(message.id, message.agent_id)
            _ = await personal_burst_dispatch_dao.mark_completed(batch.gateway_id)
        else:
            _ = await personal_burst_dispatch_dao.cancel_batch(batch.gateway_id, reason)
        logger.info("[OpenClaw] personal burst {} cancelled at dispatch: {}", batch.gateway_id, reason)
        return _CANCELLED
    if batch.state == "claimed":
        return await _claim_stored(request, access, message)
    sealed = batch if batch.state == "sealed" else await personal_burst_dispatch_dao.seal(batch.gateway_id)
    if sealed is None or sealed.state == "cancelled":
        return _CANCELLED
    if sealed.state != "sealed" or sealed.frozen_body is None or access.agent is None:
        return _blocked("not_due")
    return await _claim_joined(request, access.agent, message, sealed)


async def _claim_joined(
    request: SharedDispatchRequest, agent: AgentRecord, message: GatewayMessageRecord, sealed: PersonalBurstBatch
) -> SharedClaimResult:
    """First dispatch of a Personal batch: route the final joined body now, then deliver it."""
    body = sealed.frozen_body or ""
    try:
        route = await choose_route(agent, body, fresh=True)
    except NoCompanyModelError:
        return _model_unavailable(message, agent)
    claim = SharedClaim(
        message.id,
        agent.id,
        message.conversation_id or "",
        body,
        route.slot,
        route.ref,
        route.complexity,
        route.reason,
        request.now,
    )
    return await _deliver(agent, claim, ResolvedModel(route.bundle, route.model), sealed)


async def _claim_stored(
    request: SharedDispatchRequest, access: LaneAccess, message: GatewayMessageRecord
) -> SharedClaimResult:
    """Legacy/other-entry rows (and a released, already frozen batch) run on EXACTLY their stored selection."""
    agent = access.agent
    if agent is None:
        return _blocked("agent_unavailable")
    resolved: ResolvedModel | None = None
    if message.guest_model_ref is not None:
        resolved = await resolve_stored_model(agent, message.guest_model_ref)
        if resolved is None:
            return _model_unavailable(message, agent)
    claim = SharedClaim(
        message.id,
        agent.id,
        message.conversation_id or "",
        message.content,
        message.selected_slot,
        message.guest_model_ref,
        message.complexity,
        message.routing_reason,
        request.now,
    )
    return await _deliver(agent, claim, resolved, None)


def _model_unavailable(message: GatewayMessageRecord, agent: AgentRecord) -> SharedClaimResult:
    logger.warning("[OpenClaw] shared turn {} left pending for {}: model unavailable", message.id, agent.id)
    return _blocked("model_unavailable")


async def _deliver(
    agent: AgentRecord, claim: SharedClaim, resolved: ResolvedModel | None, sealed: PersonalBurstBatch | None
) -> SharedClaimResult:
    """Apply the guest model (lock held), then body/routing/delivered + batch claimed in this one transaction."""
    if resolved is not None:
        try:
            apply_guest_model(agent, resolved.bundle, resolved.model)
        except GuestConfigWriteError:
            logger.warning(
                "[OpenClaw] shared turn {} left pending for {}: guest config unwritten", claim.message_id, agent.id
            )
            return _blocked("config_unwritable")
    claimed = await gateway_message_dao.claim_head(claim)
    if claimed is None:
        return _blocked("lost")
    if sealed is not None and not await personal_burst_dispatch_dao.mark_claimed(sealed.gateway_id):
        raise SharedBatchStateError(sealed.gateway_id)
    return SharedClaimResult(claimed)
