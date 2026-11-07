"""Transactional OpenClaw delivery claims and first-report-wins publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException

from app.core.acl_locks import lock_agent, lock_tenants, lock_user
from app.core.logging import logger
from app.core.row_memo import memo_drop
from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao
from app.dao.participant_dao import participant_dao
from app.dao.personal_burst_dispatch_dao import personal_burst_dispatch_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.schemas.chat_wire import ChatEventType
from app.services.channels.types import normalize_channel_type
from app.services.gateway_report_wake import wake_gateway_report_relay
from app.services.openclaw_gateway_stream import GatewayDelta
from app.services.response_identity import response_guard
from app.services.shared_chat import shared_session_id

CHUNK_SEND_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class RecordedReport:
    message: GatewayMessageRecord
    assistant_message_id: UUID | None


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def settle_native_report(
    agent: AgentRecord, message_id: UUID, result: str, *, binding_id: UUID | None = None
) -> RecordedReport | None:
    """Take scope locks before the Shared session lock and reject a stale execution owner.

    ``binding_id`` additionally requires the row to still carry that reply binding: revocation nulls it.
    """
    try:
        async with connection_ctx() as db:
            # Shared tenant/owner (see claim_ordinary_turn); the agent row stays exclusive.
            await lock_tenants(db, (agent.tenant_id,), shared=True)
            await lock_user(db, agent.creator_id, agent.tenant_id, shared=True)
            await lock_agent(db, agent.id, agent.tenant_id)
            memo_drop("agent", agent.id)
            current = await agent_dao.get(agent.id)
            if current is None or current.creator_id != agent.creator_id or current.tenant_id != agent.tenant_id:
                return None
            candidate = await gateway_message_dao.get_for_agent(message_id, current.id)
            if candidate is None or candidate.status != "delivered":
                return None
            if binding_id is not None and candidate.reply_binding_id != binding_id:
                return None
            if binding_id is not None:
                return await record_report(current, message_id, result, binding_id=binding_id)
            return await record_report(current, message_id, result)
    except HTTPException as error:
        if error.status_code in (404, 409):
            return None
        raise


async def record_report(
    agent: AgentRecord, message_id: UUID, result: str, *, now: datetime | None = None, binding_id: UUID | None = None
) -> RecordedReport | None:
    """Commit the winning completion, chat, last-seen touch, and delivery intents."""
    at = now or _utcnow()
    shared_id = shared_session_id(agent.id)
    async with connection_ctx():
        candidate = await gateway_message_dao.get_for_agent(message_id, agent.id)
        if candidate is not None and candidate.conversation_id == str(shared_id):
            # Settlement joins claims, appends and cancellations under the lane lock, and marks the burst batch
            # completed before the gateway row moves, so no path ever takes the batch and the lane in reverse.
            _ = await gateway_message_dao.lock_shared_lane(agent.id, shared_id)
            candidate = await gateway_message_dao.get_for_agent(message_id, agent.id)
            if candidate is None or candidate.status != "delivered":
                return None
            _ = await personal_burst_dispatch_dao.mark_completed(message_id)
        # Every durable and live destination reads this stored result, so concealment happens once, here.
        guard = await response_guard(agent, candidate.guest_model_ref if candidate is not None else None)
        if binding_id is None:
            won = await gateway_message_dao.complete(message_id, agent.id, result=guard.redact(result), now=at)
        else:
            won = await gateway_message_dao.complete(
                message_id, agent.id, result=guard.redact(result), now=at, binding_id=binding_id
            )
        if won is None:
            return None
        answer = won.result or ""
        assistant_message_id = None
        if won.conversation_id not in (None, "") and answer:
            participant = await participant_dao.get_by_type_ref("agent", agent.id)
            inserted = await chat_message_dao.insert_message(
                agent_id=agent.id,
                user_id=won.sender_user_id or agent.creator_id,
                role="assistant",
                content=answer,
                conversation_id=won.conversation_id,
                participant_id=participant.id if participant else None,
            )
            assistant_message_id = inserted.id
        _ = await agent_dao.update(db_obj=agent, obj_in={"openclaw_last_seen": at})
        if won.conversation_id not in (None, ""):
            try:
                session_id = UUID(won.conversation_id)
            except ValueError:
                session_id = None
            if session_id is not None:
                session = await chat_session_dao.get(session_id)
                if (
                    session is not None
                    and session.agent_id == agent.id
                    and normalize_channel_type(session.source_channel) not in (None, "", "web", "agent", "shared")
                ):
                    _ = await gateway_report_delivery_dao.create_intent(
                        message_id=won.id,
                        destination_kind="im",
                        destination_id=session.id,
                        tenant_id=agent.tenant_id,
                    )
                    wake_gateway_report_relay()
        if won.sender_agent_id is not None:
            sender = await agent_dao.get(won.sender_agent_id)
            if sender is not None and sender.tenant_id == agent.tenant_id:
                _ = await gateway_report_delivery_dao.create_intent(
                    message_id=won.id,
                    destination_kind="agent",
                    destination_id=sender.id,
                    tenant_id=agent.tenant_id,
                )
                wake_gateway_report_relay()
        from app.services.openclaw_routing import wake_shared_lane

        wake_shared_lane(agent, won.id)
    return RecordedReport(message=won, assistant_message_id=assistant_message_id)


async def publish_report(agent: AgentRecord, report: RecordedReport) -> None:
    """Best-effort WebSocket notification; durable destinations belong to the relay."""
    message = report.message
    if message.conversation_id not in (None, ""):
        try:
            from app.api.websocket import manager

            event: dict[str, object] = {
                "type": ChatEventType.DONE,
                "role": "assistant",
                "content": message.result or "",
                "turn_id": str(message.id),
            }
            if report.assistant_message_id is not None:
                event["message_id"] = str(report.assistant_message_id)
                event["entry_id"] = str(report.assistant_message_id)
            await manager.send_to_session(
                str(agent.id),
                message.conversation_id,
                event,
                user_id=(
                    str(message.sender_user_id)
                    if message.sender_user_id and message.conversation_id != str(shared_session_id(agent.id))
                    else None
                ),
            )
            logger.info("[Gateway] pushed reply to session {} agent={}", message.conversation_id, agent.id)
        except Exception as error:
            logger.warning("[Gateway] Skipped done notification for disconnected user: {}", error)


async def publish_chunk(message: GatewayMessageRecord, delta: GatewayDelta) -> None:
    """Best-effort live delivery; socket failure never changes durable execution.

    Only each socket send is deadlined. Authorization and routing run to completion: cancelling them discards the
    positive authorization decision they are about to cache, so a deadline over the whole publication starves every
    later chunk whenever those dependencies are merely slower than the deadline.
    """
    from app.api.websocket import manager

    try:
        await manager.send_to_session(
            str(message.agent_id),
            message.conversation_id or "",
            {
                "type": ChatEventType.CHUNK,
                "content": delta.text,
                "turn_id": str(message.id),
                "seq": delta.seq,
                "replace": delta.replace,
            },
            user_id=(
                str(message.sender_user_id)
                if message.sender_user_id and message.conversation_id != str(shared_session_id(message.agent_id))
                else None
            ),
            send_timeout=CHUNK_SEND_TIMEOUT_SECONDS,
        )
    except Exception as error:
        logger.warning("[Gateway] Skipped chunk notification message={} error={}", message.id, type(error).__name__)
