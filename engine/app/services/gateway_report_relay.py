"""Leased report delivery; PostgreSQL is the only durable work queue."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import assert_never

import anyio

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_session_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.chat import ChatSessionRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.gateway_report_delivery import GatewayReportDeliveryRecord
from app.services.channels.outbound import deliver_session_reply
from app.services.channels.types import CHANNEL_TYPES, normalize_channel_type
from app.services.openclaw_routing import enqueue_prepared_openclaw_message, prepare_openclaw_message


class LostClaimError(RuntimeError):
    """The lease expired or a newer worker owns this intent."""


class InvalidReportDestinationError(RuntimeError):
    """The persisted report relationship no longer permits delivery."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def retry_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(300, 1 << min(max(attempts - 1, 0), 9)))


async def _source(claim: GatewayReportDeliveryRecord) -> tuple[GatewayMessageRecord, AgentRecord]:
    message = await gateway_message_dao.get(claim.message_id)
    if message is None or message.status != "completed" or message.result is None or not message.result.strip():
        raise InvalidReportDestinationError("Completed source report is unavailable")
    agent = await agent_dao.get(message.agent_id)
    if agent is None or agent.tenant_id != claim.tenant_id:
        raise InvalidReportDestinationError("Source tenant differs from delivery snapshot")
    return message, agent


async def _agent_destination(claim: GatewayReportDeliveryRecord, message: GatewayMessageRecord) -> AgentRecord:
    agent = await agent_dao.get(claim.destination_id)
    if agent is None or agent.tenant_id != claim.tenant_id or message.sender_agent_id != agent.id:
        raise InvalidReportDestinationError("Agent destination differs from delivery snapshot")
    return agent


async def _im_destination(claim: GatewayReportDeliveryRecord, message: GatewayMessageRecord) -> ChatSessionRecord:
    session = await chat_session_dao.get(claim.destination_id)
    if session is None or session.agent_id != message.agent_id or message.conversation_id != str(session.id):
        raise InvalidReportDestinationError("IM session differs from source conversation")
    channel = CHANNEL_TYPES.get(normalize_channel_type(session.source_channel) or "")
    if channel is None or not channel.supports_inbound or not (session.external_conv_id or "").strip():
        raise InvalidReportDestinationError("IM session no longer has a usable reply channel")
    return session


async def _send_im(agent: AgentRecord, session: ChatSessionRecord, content: str) -> None:
    # Strict delivery must propagate provider failures and missing configuration.
    await deliver_session_reply(agent=agent, session=session, content=content, strict=True)


async def _acknowledge(claim: GatewayReportDeliveryRecord, clock: Callable[[], datetime]) -> None:
    if not await gateway_report_delivery_dao.acknowledge(claim, now=clock()):
        raise LostClaimError


async def _deliver_agent(claim: GatewayReportDeliveryRecord, clock: Callable[[], datetime]) -> None:
    message, _ = await _source(claim)
    destination = await _agent_destination(claim, message)
    prepared = await prepare_openclaw_message(agent=destination, content=message.result or "")
    async with connection_ctx():
        if not await gateway_report_delivery_dao.lock_claim(claim, now=clock()):
            raise LostClaimError
        message, _ = await _source(claim)
        destination = await _agent_destination(claim, message)
        if prepared.content != message.result:
            raise InvalidReportDestinationError("Report changed during preparation")
        _ = await enqueue_prepared_openclaw_message(
            replace(prepared, agent=destination),
            sender_agent_id=message.agent_id,
            conversation_id=message.conversation_id or f"gw_agent_{destination.id}_{message.agent_id}",
        )
        # Raising on a lost fence rolls the destination insert and deferred wake back.
        await _acknowledge(claim, clock)


async def _deliver_im(claim: GatewayReportDeliveryRecord, clock: Callable[[], datetime]) -> None:
    async with connection_ctx():
        if not await gateway_report_delivery_dao.lock_claim(claim, now=clock()):
            raise LostClaimError
        message, agent = await _source(claim)
        session = await _im_destination(claim, message)
    # No database connection/row lock spans the remote provider call.
    await _send_im(agent, session, message.result or "")
    async with connection_ctx():
        await _acknowledge(claim, clock)


async def deliver_claim(claim: GatewayReportDeliveryRecord, clock: Callable[[], datetime] = _utcnow) -> None:
    """One bounded attempt. Cancellation intentionally leaves the lease untouched."""
    try:
        with anyio.fail_after(45):
            match claim.destination_kind:
                case "agent":
                    await _deliver_agent(claim, clock)
                case "im":
                    await _deliver_im(claim, clock)
                case unreachable:
                    assert_never(unreachable)
    except LostClaimError:
        return
    except Exception as exc:
        logger.warning("[GatewayReportRelay] delivery failed intent={} attempt={}: {}", claim.id, claim.attempts, exc)
        now = clock()
        _ = await gateway_report_delivery_dao.retry(claim, available_at=now + retry_delay(claim.attempts), now=now)


async def relay_batch(clock: Callable[[], datetime] = _utcnow) -> int:
    claims = await gateway_report_delivery_dao.claim_batch(limit=20, lease_seconds=60, now=clock())
    # Start all bounded claims together instead of consuming the last item's lease waiting in line.
    async with anyio.create_task_group() as tasks:
        for claim in claims:
            tasks.start_soon(deliver_claim, claim, clock)
    return len(claims)


async def start_gateway_report_relay() -> None:
    """Runtime-owned worker loop; never started from a request or connector."""
    while True:
        try:
            count = await relay_batch()
        except Exception as exc:
            logger.warning("[GatewayReportRelay] batch failed: {}", exc)
            count = 0
        if count == 0:
            await anyio.sleep(1)
