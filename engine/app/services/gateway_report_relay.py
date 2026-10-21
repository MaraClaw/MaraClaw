"""Leased report delivery; PostgreSQL is the only durable work queue."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import assert_never, override

import anyio

from app.core.json_types import uuid_from_row
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
from app.services.channels.outbound import deliver_session_reply
from app.services.channels.types import CHANNEL_TYPES, normalize_channel_type
from app.services.openclaw_routing import enqueue_prepared_openclaw_message, prepare_openclaw_message


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


async def _lock_identity(claim: GatewayReportDeliveryRecord) -> None:
    async with gateway_message_dao.session() as db:
        row = await db.fetchone(
            "SELECT agent_id FROM gateway_messages WHERE id = %(id)s FOR SHARE NOWAIT",
            {"id": claim.message_id},
        )
        if row is None:
            return
        source_id = uuid_from_row(row["agent_id"])
        match claim.destination_kind:
            case "agent":
                ids = {source_id, claim.destination_id}
            case "im":
                ids = {source_id}
                _ = await db.fetchone(
                    "SELECT id FROM chat_sessions WHERE id = %(id)s FOR SHARE NOWAIT",
                    {"id": claim.destination_id},
                )
            case unreachable:
                assert_never(unreachable)
        _ = await db.fetchall(
            "SELECT id FROM agents WHERE id = ANY(%(ids)s) ORDER BY id FOR SHARE NOWAIT",
            {"ids": sorted(ids)},
        )


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
    return message, agent


async def _agent_destination(claim: GatewayReportDeliveryRecord, message: GatewayMessageRecord) -> AgentRecord:
    agent = next(iter(await agent_dao.get_many([claim.destination_id])), None)
    if agent is None:
        raise _IdentityError("destination_agent_unavailable")
    if agent.tenant_id != claim.tenant_id:
        raise _IdentityError("destination_tenant_changed")
    if message.sender_agent_id != agent.id:
        raise _IdentityError("destination_relationship_changed")
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


async def _send_im(agent: AgentRecord, session: ChatSessionRecord, content: str) -> None:
    # Strict delivery must propagate provider failures and missing configuration.
    await deliver_session_reply(agent=agent, session=session, content=content, strict=True)


async def _acknowledge(claim: GatewayReportDeliveryRecord, clock: Callable[[], datetime]) -> None:
    if not await gateway_report_delivery_dao.acknowledge(claim, now=clock()):
        raise LostClaimError


async def _deliver_agent(claim: GatewayReportDeliveryRecord, clock: Callable[[], datetime]) -> None:
    prepared = None
    try:
        message, _ = await _source(claim)
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


async def _deliver_im(claim: GatewayReportDeliveryRecord, clock: Callable[[], datetime]) -> None:
    async with connection_ctx():
        if not await gateway_report_delivery_dao.lock_claim(claim, now=clock()):
            raise LostClaimError
        await _lock_identity(claim)
        try:
            message, agent = await _source(claim)
            session = await _im_destination(claim, message)
        except _IdentityError as error:
            await _finish_identity(claim, error, clock)
            return
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
