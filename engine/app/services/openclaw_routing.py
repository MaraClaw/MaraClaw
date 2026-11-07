"""Persist model selection, then drain committed work through native Gateway RPC."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import assert_never
from uuid import UUID

from fastapi import HTTPException

from app.config import get_settings
from app.core.logging import logger
from app.dao.gateway_message_dao import gateway_message_dao
from app.db.session import connection_ctx, get_connection
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.openclaw_dispatch import PreparedOpenClawMessage, SharedDispatchRequest
from app.runtime.tasks import TaskAdmission
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
from app.services.openclaw_native_claims import claim_ordinary_turn
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
            outcome = await execute_gateway_turn(agent, claimed, timing=timeline)
        except WorkspaceLockTimeoutError, GuestConfigWriteError, OpenClawUpdateGateError:
            # These adapter boundaries refuse before sending any RPC request.
            outcome_kind = "released"
            _ = await drain_on_cancel(gateway_message_dao.release_delivery(claimed))
            return
        timeline.mark("rpc_returned")
        timeline.adapter(outcome.timing)
        outcome_kind = outcome.kind
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
        if timeline is not None:
            timeline.emit(outcome_kind)


_wake_tasks: set[asyncio.Task[None]] = set()
_wake_admission = TaskAdmission("Inbox wake", _wake_tasks)
_wake_running: set[UUID] = set()
_wake_again: set[UUID] = set()
_lane_running: set[tuple[UUID, str]] = set()
_lane_again: set[tuple[UUID, str]] = set()
_wake_semaphores: dict[UUID, asyncio.Semaphore] = {}
_LANE_HEAD_PAGE = 50


def _forget_idle_semaphore(agent_id: UUID) -> None:
    if agent_id not in _wake_running and not any(key[0] == agent_id for key in _lane_running):
        _wake_semaphores.pop(agent_id, None)


def _start_lane(agent: AgentRecord, pending: GatewayMessageRecord) -> None:
    key = (agent.id, pending.conversation_id or str(pending.id))
    if key in _lane_running:
        _lane_again.add(key)
        return
    _lane_running.add(key)
    semaphore = _wake_semaphores.setdefault(
        agent.id, asyncio.Semaphore(get_settings().OPENCLAW_AGENT_MAX_PARALLEL_TURNS)
    )

    async def run_lane() -> None:
        row = pending
        try:
            while True:
                async with semaphore:
                    if not _wake_admission.accepting:
                        break
                    await _run_inbox_wake(agent, row.content, row.id)
                if key not in _lane_again:
                    break
                # Consume before re-listing so a wake during that read survives the next turn.
                while True:
                    _lane_again.discard(key)
                    head = await gateway_message_dao.lane_head(row)
                    if head is not None or key not in _lane_again:
                        break
                if head is None:
                    break
                row = head
        except Exception:
            logger.warning("[OpenClaw] native lane failed agent={} code=drain_failed", agent.id)
        finally:
            _lane_running.discard(key)
            _lane_again.discard(key)
            _forget_idle_semaphore(agent.id)

    task = asyncio.create_task(run_lane())
    _wake_tasks.add(task)
    task.add_done_callback(_wake_tasks.discard)


def open_wake_admission() -> None:
    """Reopen inbox wake admission for a new lifespan."""
    _wake_admission.open()


def close_wake_admission() -> None:
    """Stop admitting native drains; refused wakes remain durable pending work."""
    _wake_admission.close()


async def drain_wake_tasks(grace_seconds: float = 20.0) -> None:
    """Join owned turns, including repeated-cancel cleanup, before runtime resources close."""
    await drain_on_cancel(_wake_admission.drain(grace_seconds))


def wake_shared_lane(agent: AgentRecord, message_id: UUID) -> None:
    """Accelerate committed work; pending-only recovery also uses this drain."""
    _schedule_inbox_wake(agent, "", message_id)


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
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if agent.id in _wake_running:
        _wake_again.add(agent.id)
        return

    _wake_running.add(agent.id)

    async def _run() -> None:
        try:
            while True:
                _wake_again.discard(agent.id)
                attempted: set[str] = set()
                after: tuple[datetime, UUID] | None = None
                while True:
                    heads, after = (
                        await gateway_message_dao.lane_heads_page(agent.id, _LANE_HEAD_PAGE, after=after)
                        if _wake_admission.accepting
                        else ([], None)
                    )
                    for pending in heads:
                        if not _wake_admission.accepting:
                            break
                        lane = pending.conversation_id or str(pending.id)
                        if lane in attempted:
                            continue
                        attempted.add(lane)
                        _start_lane(agent, pending)
                    if after is None:
                        break
                if agent.id not in _wake_again or not _wake_admission.accepting:
                    break
        except Exception:
            logger.warning("[OpenClaw] native drain failed agent={} code=drain_failed", agent.id)
        finally:
            _wake_running.discard(agent.id)
            _wake_again.discard(agent.id)
            _forget_idle_semaphore(agent.id)

    task = loop.create_task(_run())
    _wake_tasks.add(task)
    task.add_done_callback(_wake_tasks.discard)
