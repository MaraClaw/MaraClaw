"""Persist model selection, then drain committed work through native Gateway RPC."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import assert_never
from uuid import UUID

from fastapi import HTTPException

from app.core.logging import logger
from app.dao.gateway_message_dao import gateway_message_dao
from app.db.session import connection_ctx, get_connection
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.openclaw_dispatch import PreparedOpenClawMessage, SharedDispatchRequest
from app.services.agent_manager import guest_model_ref
from app.services.agent_setup_lock import WorkspaceLockTimeoutError, drain_on_cancel
from app.services.agent_work_eligibility import load_agent_for_new_work
from app.services.enterprise_llm import ensure_agent_company_models
from app.services.llm.router import load_agent_model_bundle, select_turn_model
from app.services.llm.types import OpenAIMessage
from app.services.openclaw_enqueue import insert_prepared_message as _insert_prepared_message
from app.services.openclaw_guest_config import GuestConfigWriteError
from app.services.openclaw_hot_cache import (
    get_cached_bundle,
    mark_ensured,
    recently_ensured,
    set_cached_bundle,
)
from app.services.openclaw_model_selection import (
    NoCompanyModelError,
    bundle_usable,
)
from app.services.openclaw_native_capacity import (
    LaneKey,
    discard_waiting_lanes,
    release_turn,
    shutdown_native_capacity,
)
from app.services.openclaw_native_claims import claim_ordinary_turn
from app.services.openclaw_native_dispatch import NativeDispatcher
from app.services.openclaw_release_backoff import release_backoff
from app.services.openclaw_shared_dispatch import claim_ready_shared
from app.services.openclaw_update_gate import OpenClawUpdateGateError, assert_work_allowed
from app.services.quota_guard import AgentExpired
from app.services.shared_chat import shared_session_id
from app.services.turn_timing import TurnOutcomeKind, TurnTimeline


async def prepare_openclaw_message(
    *,
    agent: AgentRecord,
    content: str,
    history: list[OpenAIMessage] | None = None,
) -> PreparedOpenClawMessage:
    """Ensure models and select routing metadata before queue insertion.

    Raises ``OpenClawUpdateGateError`` before any config or model side effect.
    """
    await assert_work_allowed(agent.id)
    agent = await load_agent_for_new_work(agent.id)
    if not recently_ensured(agent):
        agent = await ensure_agent_company_models(agent)
    bundle = get_cached_bundle(agent)
    if bundle is None:
        bundle = await load_agent_model_bundle(agent)
    if not bundle_usable(bundle):
        raise NoCompanyModelError(
            "This company has no model assigned. Connect a Grok or ChatGPT subscription or add a model in Admin → Models, "
            "then set it as primary."
        )
    mark_ensured(agent)
    set_cached_bundle(agent, bundle)
    choice = await select_turn_model(
        bundle,
        user_text=content,
        history=history,
        agent_id=agent.id,
        skip_classifier=True,
    )
    return PreparedOpenClawMessage(
        agent, content, choice.slot, guest_model_ref(choice.model), choice.complexity, choice.reason
    )


async def enqueue_prepared_openclaw_message(
    prepared: PreparedOpenClawMessage,
    *,
    sender_user_id: UUID | None = None,
    sender_agent_id: UUID | None = None,
    conversation_id: str | None = None,
    deduplicate: bool = True,
    reply_session_id: UUID | None = None,
) -> GatewayMessageRecord:
    """Queue only; wake in a fresh context after the owning transaction commits.

    The row and caller's other writes share the owning transaction;
    rollback drops the deferred wake.
    """
    async with connection_ctx():
        created = await _insert_prepared_message(
            prepared,
            sender_user_id=sender_user_id,
            sender_agent_id=sender_agent_id,
            conversation_id=conversation_id,
            deduplicate=deduplicate,
            reply_session_id=reply_session_id,
        )
        _schedule_inbox_wake(prepared.agent, prepared.content, created.id)
    return created


async def enqueue_openclaw_message(
    *,
    agent: AgentRecord,
    content: str,
    sender_user_id: UUID | None = None,
    sender_agent_id: UUID | None = None,
    conversation_id: str | None = None,
    history: list[OpenAIMessage] | None = None,
    reply_session_id: UUID | None = None,
) -> GatewayMessageRecord:
    """Prepare and commit a normal turn without waiting for inference."""
    prepared = await prepare_openclaw_message(agent=agent, content=content, history=history)
    async with connection_ctx():
        created = await _insert_prepared_message(
            prepared,
            sender_user_id=sender_user_id,
            sender_agent_id=sender_agent_id,
            conversation_id=conversation_id,
            reply_session_id=reply_session_id,
        )
    _schedule_inbox_wake(prepared.agent, content, created.id)
    return created


async def _claim_wake(agent: AgentRecord, message_id: UUID) -> GatewayMessageRecord | None:
    """Ordinary claims name a row; Shared always claims its actual ready FIFO head."""
    requested = await gateway_message_dao.get_for_agent(message_id, agent.id)
    if requested is None:
        return None
    if requested.conversation_id != str(shared_session_id(agent.id)):
        return await claim_ordinary_turn(agent, message_id)
    return await claim_ready_shared(SharedDispatchRequest(agent.id, datetime.now(UTC)))


async def _run_inbox_wake(agent: AgentRecord, _content: str, message_id: UUID | None = None) -> None:
    if message_id is None:
        return
    claimed: GatewayMessageRecord | None = None
    timeline: TurnTimeline | None = None
    outcome_kind: TurnOutcomeKind = "failed"
    try:
        from app.services.openclaw_gateway_delivery import publish_report, settle_native_report
        from app.services.openclaw_gateway_rpc import (
            NotAdmittedOutcome,
            TerminalOutcome,
            UncertainOutcome,
            execute_gateway_turn,
        )

        try:
            claimed = await _claim_wake(agent, message_id)
        except OpenClawUpdateGateError as gate:
            # The claim checks the gate in its transaction, leaving refused work pending.
            logger.info("[OpenClaw] inbox wake left pending for {}: {}", agent.id, gate.code)
            return
        except AgentExpired, HTTPException:
            return
        if claimed is None:
            return
        timeline = TurnTimeline.claimed(claimed)
        try:
            try:
                outcome = await execute_gateway_turn(agent, claimed, timing=timeline)
            finally:
                release_turn()
        except WorkspaceLockTimeoutError, GuestConfigWriteError, OpenClawUpdateGateError:
            # These adapter boundaries refuse before sending any RPC request.
            outcome_kind = "released"
            _ = release_backoff.released(LaneKey.of(claimed))
            _ = await drain_on_cancel(gateway_message_dao.release_delivery(claimed))
            return
        timeline.mark("rpc_returned")
        timeline.adapter(outcome.timing)
        outcome_kind = outcome.kind
        if isinstance(outcome, NotAdmittedOutcome):
            _ = release_backoff.released(LaneKey.of(claimed))
        else:
            release_backoff.settled(LaneKey.of(claimed))
        match outcome:
            case NotAdmittedOutcome():
                _ = await drain_on_cancel(gateway_message_dao.release_delivery(claimed))
            case TerminalOutcome(status=status, text=text):
                match status:
                    case "ok":
                        result = text or "The agent completed the request without a text response."
                    case "error":
                        result = "The agent could not complete this request."
                    case "timeout":
                        result = "The agent timed out before completing this request."
                    case unreachable:
                        assert_never(unreachable)
                report = await settle_native_report(agent, claimed.id, result)
                timeline.mark("settled")
                if report is not None:
                    await publish_report(agent, report)
                    timeline.mark("published")
            case UncertainOutcome():
                logger.warning("[OpenClaw] admission uncertain for message {}", claimed.id)
            case unreachable:
                assert_never(unreachable)
    except asyncio.CancelledError:
        outcome_kind = "cancelled"
        raise
    except Exception:
        # Admission is unknown after an unexpected boundary failure. Retain every lane's claim.
        outcome_kind = "failed"
        logger.warning("[OpenClaw] native dispatch failed agent={} code=dispatch_failed", agent.id)
    finally:
        release_turn()
        if timeline is not None:
            timeline.emit(outcome_kind)


_dispatcher = NativeDispatcher(
    lambda agent, text, message_id: _run_inbox_wake(agent, text, message_id), lambda: _LANE_HEAD_PAGE
)
_wake_tasks = _dispatcher.tasks
_wake_admission = _dispatcher.admission
_wake_running = _dispatcher.scanning
_wake_again = _dispatcher.scan_again
_lane_running = _dispatcher.running
_lane_again = _dispatcher.again
_LANE_HEAD_PAGE = 50
_start_lane = _dispatcher.start


def open_wake_admission() -> None:
    """Reopen inbox wake admission for a new lifespan."""
    _wake_admission.open()


def close_wake_admission() -> None:
    """Stop admitting native drains; refused wakes remain durable pending work."""
    _wake_admission.close()
    discard_waiting_lanes()


async def drain_wake_tasks(grace_seconds: float = 20.0) -> None:
    """Join owned turns, including repeated-cancel cleanup, before runtime resources close."""
    close_wake_admission()
    try:
        await drain_on_cancel(_wake_admission.drain(grace_seconds))
    finally:
        shutdown_native_capacity()


def wake_shared_lane(agent: AgentRecord, message_id: UUID) -> None:
    """Accelerate committed work; pending-only recovery also uses this drain."""
    _schedule_inbox_wake(agent, "", message_id)


def wake_agent_lanes(agent: AgentRecord) -> None:
    """Rescan every pending lane head of ``agent`` in this process (after commit inside a transaction).

    Safe without a running event loop or during shutdown: the wake is then skipped and the worker's
    one-second dispatch reconciler picks the pending heads up instead.
    """
    _schedule_inbox_wake(agent, "")


def _schedule_inbox_wake(agent: AgentRecord, content: str, message_id: UUID | None = None) -> None:
    """Start inbox wake without blocking the chat socket."""
    if not _wake_admission.accepting:
        logger.info("[OpenClaw] inbox wake left pending for {}: shutdown drain in progress", agent.id)
        return
    if get_connection() is not None:
        from app.api.background_tasks import schedule_background_task

        async def after_commit() -> None:
            _schedule_inbox_wake(agent, content, message_id)

        schedule_background_task(after_commit(), description="wake committed OpenClaw inbox")
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    _dispatcher.wake(agent)
