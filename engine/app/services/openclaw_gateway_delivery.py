"""Transactional OpenClaw delivery claims and first-report-wins publication."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.config import get_settings
from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.participant_dao import participant_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services.openclaw_routing import enqueue_openclaw_message


def _utcnow() -> datetime:
    return datetime.now(UTC)


def delivery_cutoff(now: datetime) -> datetime:
    return now - timedelta(seconds=get_settings().OPENCLAW_DELIVERY_TIMEOUT_SECONDS)


async def claim_poll_batch(agent_id: UUID, *, now: datetime | None = None) -> list[GatewayMessageRecord]:
    """Requeue expired deliveries and atomically claim the pending batch."""
    at = now or _utcnow()
    async with connection_ctx():
        requeued = await gateway_message_dao.requeue_stale_deliveries(agent_id, stale_before=delivery_cutoff(at))
        messages = await gateway_message_dao.claim_pending(agent_id, now=at)
    if requeued:
        logger.info("[Gateway] Requeued stale deliveries agent={} ids={}", agent_id, requeued)
    return messages


async def record_report(
    agent: AgentRecord, message_id: UUID, result: str, *, now: datetime | None = None
) -> GatewayMessageRecord | None:
    """Commit the winning completion, assistant message, and last-seen touch together."""
    at = now or _utcnow()
    async with connection_ctx():
        won = await gateway_message_dao.complete(message_id, agent.id, result=result, now=at)
        if won is None:
            return None
        if won.conversation_id and result:
            participant = await participant_dao.get_by_type_ref("agent", agent.id)
            _ = await chat_message_dao.insert_message(
                agent_id=agent.id,
                user_id=won.sender_user_id or agent.creator_id,
                role="assistant",
                content=result,
                conversation_id=won.conversation_id,
                participant_id=participant.id if participant else None,
            )
        _ = await agent_dao.update(db_obj=agent, obj_in={"openclaw_last_seen": at})
    return won


async def publish_report(agent: AgentRecord, message: GatewayMessageRecord, result: str) -> None:
    """Fan out a committed winning report without coupling publisher failures."""
    if message.conversation_id:
        try:
            from app.api.websocket import manager

            await manager.send_to_session(
                str(agent.id),
                message.conversation_id,
                {"type": "done", "role": "assistant", "content": result},
                user_id=str(message.sender_user_id) if message.sender_user_id else None,
            )
            logger.info("[Gateway] pushed reply to session {} agent={}", message.conversation_id, agent.id)
        except Exception as error:
            logger.warning("[Gateway] Skipped done notification for disconnected user: {}", error)

        try:
            try:
                session = await chat_session_dao.get(UUID(message.conversation_id))
            except ValueError, TypeError:
                session = None
            if session is not None:
                from app.services.channels.outbound import deliver_session_reply

                await deliver_session_reply(agent=agent, session=session, content=result)
        except Exception as error:
            logger.warning("[Gateway] Skipped IM reply message={}: {}", message.id, error)

    if message.sender_agent_id:
        try:
            conv_id = message.conversation_id or f"gw_agent_{message.sender_agent_id}_{agent.id}"
            sender = await agent_dao.get(message.sender_agent_id)
            if sender:
                _ = await enqueue_openclaw_message(
                    agent=sender,
                    content=result,
                    sender_agent_id=agent.id,
                    conversation_id=conv_id,
                    await_wake=False,
                )
            logger.info("[Gateway] Reply routed back to sender agent {}", message.sender_agent_id)
        except Exception as error:
            logger.warning("[Gateway] Skipped sender-agent reply message={}: {}", message.id, error)
