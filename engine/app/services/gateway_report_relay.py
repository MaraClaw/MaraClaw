"""Leased report delivery; PostgreSQL is the only durable work queue.

Provider acceptance before a lost checkpoint can still repeat the current chunk.
Supported provider keys mitigate that ambiguity, not guarantee exactly-once delivery.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from secrets import randbelow
from typing import Final, assert_never, override

import anyio
from anyio.abc import TaskGroup

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_session_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.chat import ChatSessionRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.gateway_report_delivery import GatewayReportDeliveryRecord, TerminalReason
from app.records.outbound_plan import OutboundProvider
from app.runtime.tasks import loop_progress
from app.services.agent_work_eligibility import agent_work_blocking_codes
from app.services.channels.outbound_errors import ProviderPayloadRejectedError, retry_at
from app.services.channels.outbound_identity import preflight_outbound
from app.services.channels.outbound_plan import build_plan, configured_outbound
from app.services.channels.outbound_transport import send_chunk
from app.services.channels.types import CHANNEL_TYPES, normalize_channel_type
from app.services.channels.whatsapp_window import WhatsAppReplyWindowError
from app.services.gateway_report_relay_identity import PresendIdentityContentionError, lock_identity as _lock_identity
from app.services.gateway_report_wake import listen, subscribe_wakes
from app.services.openclaw_routing import enqueue_prepared_openclaw_message, prepare_openclaw_message
from app.services.turn_timing import RelayTimeline


class LostClaimError(RuntimeError):
    """The lease expired or a newer worker owns this intent."""


class InvalidReportDestinationError(RuntimeError):
    """The persisted report relationship no longer permits delivery."""


@dataclass
class _IdentityError(Exception):
    reason: TerminalReason

    @override
    def __str__(self) -> str:
        return self.reason


async def _finish_identity(
    claim: GatewayReportDeliveryRecord, error: _IdentityError, clock: Callable[[], datetime]
) -> None:
    if claim.claim_token is None or not await gateway_report_delivery_dao.finish_terminal(
        claim.id, claim.claim_token, error.reason, now=clock()
    ):
        raise LostClaimError


def _utcnow() -> datetime:
    return datetime.now(UTC)


def retry_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(300, 1 << min(max(attempts - 1, 0), 9)))


async def _source(claim: GatewayReportDeliveryRecord) -> tuple[GatewayMessageRecord, AgentRecord]:
    message = await gateway_message_dao.get(claim.message_id)
    if message is None or message.status != "completed" or message.result is None or not message.result.strip():
        raise InvalidReportDestinationError("Completed source report is unavailable")
    agent = next(iter(await agent_dao.get_many([message.agent_id])), None)
    if agent is None:
        raise _IdentityError("source_agent_unavailable")
    if agent.tenant_id != claim.tenant_id:
        raise _IdentityError("source_tenant_changed")
    codes = await agent_work_blocking_codes(agent)
    if codes:
        raise InvalidReportDestinationError("Source agent work blocked: " + ",".join(codes))
    return message, agent


async def _agent_destination(claim: GatewayReportDeliveryRecord, message: GatewayMessageRecord) -> AgentRecord:
    agent = next(iter(await agent_dao.get_many([claim.destination_id])), None)
    if agent is None:
        raise _IdentityError("destination_agent_unavailable")
    if agent.tenant_id != claim.tenant_id:
        raise _IdentityError("destination_tenant_changed")
    if message.sender_agent_id != agent.id:
        raise _IdentityError("destination_relationship_changed")
    codes = await agent_work_blocking_codes(agent)
    if codes:
        raise InvalidReportDestinationError("Destination agent work blocked: " + ",".join(codes))
    return agent


async def _im_destination(claim: GatewayReportDeliveryRecord, message: GatewayMessageRecord) -> ChatSessionRecord:
    session = await chat_session_dao.get(claim.destination_id)
    if session is None:
        raise _IdentityError("destination_session_unavailable")
    if session.agent_id != message.agent_id:
        raise _IdentityError("destination_relationship_changed")
    channel = CHANNEL_TYPES.get(normalize_channel_type(session.source_channel) or "")
    if channel is None or not channel.supports_inbound or not (session.external_conv_id or "").strip():
        raise InvalidReportDestinationError("IM session no longer has a usable reply channel")
    return session


async def _acknowledge(claim: GatewayReportDeliveryRecord, clock: Callable[[], datetime]) -> None:
    if not await gateway_report_delivery_dao.acknowledge(claim, now=clock()):
        raise LostClaimError


async def _deliver_agent(
    claim: GatewayReportDeliveryRecord, clock: Callable[[], datetime], timing: RelayTimeline
) -> None:
    prepared = None
    try:
        message, _ = await _source(claim)
        timing.completed_at = message.completed_at
        destination = await _agent_destination(claim, message)
    except _IdentityError:
        pass
    else:
        prepared = await prepare_openclaw_message(agent=destination, content=message.result or "")
    async with connection_ctx():
        if not await gateway_report_delivery_dao.lock_claim(claim, now=clock()):
            raise LostClaimError
        await _lock_identity(claim)
        try:
            message, _ = await _source(claim)
            destination = await _agent_destination(claim, message)
        except _IdentityError as error:
            await _finish_identity(claim, error, clock)
            timing.result = "terminal"
            return
        if prepared is None:
            raise InvalidReportDestinationError("Identity recovered; preparation deferred")
        if prepared.content != message.result:
            raise InvalidReportDestinationError("Report changed during preparation")
        _ = await enqueue_prepared_openclaw_message(
            replace(prepared, agent=destination),
            sender_agent_id=message.agent_id,
            conversation_id=message.conversation_id or f"gw_agent_{destination.id}_{message.agent_id}",
        )
        # Raising on a lost fence rolls the destination insert and deferred wake back.
        await _acknowledge(claim, clock)
        timing.result = "acknowledged"


async def _deliver_im(claim: GatewayReportDeliveryRecord, clock: Callable[[], datetime], timing: RelayTimeline) -> None:
    prepared = None
    if claim.im_plan is None or claim.next_chunk < len(claim.im_plan.chunks):
        try:
            message, agent = await _source(claim)
            timing.completed_at = message.completed_at
            session = await _im_destination(claim, message)
        except _IdentityError:
            pass
        else:
            prepared = await preflight_outbound(await configured_outbound(agent, session))
    async with connection_ctx():
        if not await gateway_report_delivery_dao.lock_claim(claim, now=clock()):
            raise LostClaimError
        if claim.im_plan is not None and claim.next_chunk == len(claim.im_plan.chunks):
            await _acknowledge(claim, clock)
            timing.result = "acknowledged"
            return
        await _lock_identity(claim)
        try:
            message, agent = await _source(claim)
            session = await _im_destination(claim, message)
        except _IdentityError as error:
            await _finish_identity(claim, error, clock)
            timing.result = "terminal"
            return
        current = await configured_outbound(agent, session)
        if prepared is None or current != prepared.snapshot:
            raise InvalidReportDestinationError("IM configuration or credentials changed during preflight")
        outbound = replace(prepared.snapshot, target=prepared.target)
        plan = claim.im_plan
        if plan is None:
            plan = build_plan(outbound.target, message.result or "", claim.id)
            if not await gateway_report_delivery_dao.initialize_plan(claim, plan, now=clock()):
                raise LostClaimError
        elif plan.target != outbound.target:
            raise InvalidReportDestinationError("IM configuration or destination changed after planning")
        if not await gateway_report_delivery_dao.lock_claim(claim, now=clock()):
            raise LostClaimError
    if outbound.target.provider == OutboundProvider.WHATSAPP:
        await send_chunk(outbound, plan.chunks[claim.next_chunk], clock=clock)
    else:
        await send_chunk(outbound, plan.chunks[claim.next_chunk])
    timing.mark("sent")
    async with connection_ctx():
        if not await gateway_report_delivery_dao.checkpoint_chunk(claim, now=clock()):
            raise LostClaimError
    timing.result = "acknowledged" if claim.next_chunk + 1 == len(plan.chunks) else "chunk_sent"


async def deliver_claim(claim: GatewayReportDeliveryRecord, clock: Callable[[], datetime] = _utcnow) -> None:
    """One bounded attempt. Cancellation intentionally leaves the lease untouched."""
    from app.services.channels.whatsapp_personal_journal import PersonalSendPendingError, PersonalSendTerminalError

    timing = RelayTimeline(claim.message_id, claim.id, claim.destination_kind, claim.attempts)
    timing.mark("claimed")
    try:
        with anyio.fail_after(45):
            match claim.destination_kind:
                case "agent":
                    await _deliver_agent(claim, clock, timing)
                case "im":
                    await _deliver_im(claim, clock, timing)
                case unreachable:
                    assert_never(unreachable)
    except LostClaimError:
        timing.result = "lost"
    except PresendIdentityContentionError as error:
        timing.result = "retry"
        logger.warning(
            "[GatewayReportRelay] identity contention intent={} attempt={} stage={} sqlstate=55P03",
            claim.id,
            claim.attempts,
            error.stage,
        )
        now = clock()
        _ = await gateway_report_delivery_dao.retry(
            claim, available_at=now + timedelta(milliseconds=1000 + randbelow(1001)), now=now
        )
    except PersonalSendTerminalError:
        timing.result = "terminal"
        if claim.claim_token is not None:
            _ = await gateway_report_delivery_dao.finish_terminal(
                claim.id, claim.claim_token, "payload_rejected", now=clock()
            )
    except PersonalSendPendingError:
        timing.result = "retry"
        now = clock()
        _ = await gateway_report_delivery_dao.retry(claim, available_at=now + retry_delay(1), now=now)
    except WhatsAppReplyWindowError as error:
        timing.result = "terminal"
        if claim.claim_token is not None:
            _ = await gateway_report_delivery_dao.finish_terminal(
                claim.id, claim.claim_token, error.reason, now=clock()
            )
    except ProviderPayloadRejectedError:
        timing.result = "terminal"
        if claim.claim_token is not None:
            _ = await gateway_report_delivery_dao.finish_terminal(
                claim.id, claim.claim_token, "payload_rejected", now=clock()
            )
    except Exception as exc:
        timing.result = "retry"
        logger.warning("[GatewayReportRelay] delivery failed intent={} attempt={}: {}", claim.id, claim.attempts, exc)
        now = clock()
        available_at = max(now + retry_delay(claim.attempts), retry_at(exc, now) or now)
        _ = await gateway_report_delivery_dao.retry(claim, available_at=available_at, now=now)
    finally:
        timing.emit()


MAX_IN_FLIGHT = 20
PROGRESS_BUDGET_SECONDS: Final = 300.0


async def _run_claim(
    claim: GatewayReportDeliveryRecord, clock: Callable[[], datetime], slots: anyio.Semaphore, done: Callable[[], None]
) -> None:
    try:
        await deliver_claim(claim, clock)
    except Exception as exc:
        logger.warning("[GatewayReportRelay] delivery task failed intent={}: {}", claim.id, exc)
    finally:
        slots.release()
        done()


async def _claim_into(
    tasks: TaskGroup, slots: anyio.Semaphore, clock: Callable[[], datetime], done: Callable[[], None]
) -> list[GatewayReportDeliveryRecord]:
    """Wait for a free slot, then claim up to every free slot and start those deliveries without awaiting them."""
    await slots.acquire()
    free = 1
    for _ in range(slots.value):  # only this caller acquires, so the count cannot shrink underneath it
        slots.acquire_nowait()
        free += 1
    try:
        claims = await gateway_report_delivery_dao.claim_batch(limit=free, lease_seconds=60, now=clock())
    except BaseException:
        for _ in range(free):
            slots.release()
        raise
    for _ in range(free - len(claims)):
        slots.release()
    # Start all bounded claims together instead of consuming the last item's lease waiting in line.
    for claim in claims:
        tasks.start_soon(_run_claim, claim, clock, slots, done)
    return claims


def _no_wake() -> None:
    return None


async def relay_batch(clock: Callable[[], datetime] = _utcnow) -> int:
    """One claim of up to MAX_IN_FLIGHT deliveries, awaited to completion."""
    claims = list[GatewayReportDeliveryRecord]()
    async with anyio.create_task_group() as tasks:
        claims = await _claim_into(tasks, anyio.Semaphore(MAX_IN_FLIGHT), clock, _no_wake)
    return len(claims)


async def start_gateway_report_relay(*, idle_timeout: float = 1.0, clock: Callable[[], datetime] = _utcnow) -> None:
    """Runtime-owned worker loop; never started from a request or connector.

    Claims continuously with at most MAX_IN_FLIGHT deliveries running. When nothing is due it waits for a wake
    (same-process signal or Redis subscriber) and otherwise polls every ``idle_timeout`` seconds.
    """
    progress = loop_progress()
    slots = anyio.Semaphore(MAX_IN_FLIGHT)
    with listen() as wake:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(subscribe_wakes)
            while True:
                # Clear before claiming: a wake set after this point is a commit the claim below may have missed.
                wake.clear()
                try:
                    count = len(await _claim_into(tasks, slots, clock, wake.set))
                    progress.beat()
                except Exception as exc:
                    logger.warning("[GatewayReportRelay] batch failed: {}", exc)
                    count = 0
                if count == 0:
                    with anyio.move_on_after(idle_timeout):
                        _ = await wake.wait()
