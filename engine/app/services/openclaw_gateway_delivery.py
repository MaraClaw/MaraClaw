"""Transactional OpenClaw delivery claims and first-report-wins publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.config import get_settings
from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao
from app.dao.participant_dao import participant_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services.channels.types import normalize_channel_type
from app.services.openclaw_update_gate import assert_work_allowed
from app.services.shared_chat import shared_session_id


@dataclass(frozen=True, slots=True)
class RecordedReport:
    message: GatewayMessageRecord
    assistant_message_id: UUID | None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def delivery_cutoff(now: datetime) -> datetime:
    return now - timedelta(seconds=get_settings().OPENCLAW_DELIVERY_TIMEOUT_SECONDS)


async def claim_poll_batch(agent_id: UUID, *, now: datetime | None = None) -> list[GatewayMessageRecord]:
    """Requeue expired deliveries and atomically claim the pending batch.

    Raises ``OpenClawUpdateGateError`` before any requeue or claim.
    """
    await assert_work_allowed(agent_id)
    at = now or _utcnow()
    shared_id = shared_session_id(agent_id)
    async with connection_ctx():
        shared = await gateway_message_dao.claim_shared(agent_id, shared_id, now=at)
        requeued = await gateway_message_dao.requeue_stale_deliveries(
            agent_id, stale_before=delivery_cutoff(at), exclude_conversation=str(shared_id)
        )
        messages = await gateway_message_dao.claim_pending(agent_id, now=at, exclude_conversation=str(shared_id))
        if shared is not None:
            messages.append(shared)
    if requeued:
        logger.info("[Gateway] Requeued stale deliveries agent={} ids={}", agent_id, requeued)
    return messages


async def record_report(
    agent: AgentRecord, message_id: UUID, result: str, *, now: datetime | None = None
) -> RecordedReport | None:
    """Commit the winning completion, chat, last-seen touch, and delivery intents."""
    at = now or _utcnow()
    async with connection_ctx():
        candidate = await gateway_message_dao.get_for_agent(message_id, agent.id)
        if (
            candidate is not None
            and candidate.conversation_id == str(shared_session_id(agent.id))
            and candidate.status != "delivered"
        ):
            return None
        won = await gateway_message_dao.complete(message_id, agent.id, result=result, now=at)
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
        if won.sender_agent_id is not None:
            sender = await agent_dao.get(won.sender_agent_id)
            if sender is not None and sender.tenant_id == agent.tenant_id:
                _ = await gateway_report_delivery_dao.create_intent(
                    message_id=won.id,
                    destination_kind="agent",
                    destination_id=sender.id,
                    tenant_id=agent.tenant_id,
                )
        if won.conversation_id == str(shared_session_id(agent.id)):
            from app.services.openclaw_routing import _schedule_inbox_wake

            _schedule_inbox_wake(agent, "", won.id)
    return RecordedReport(message=won, assistant_message_id=assistant_message_id)


async def publish_report(agent: AgentRecord, report: RecordedReport) -> None:
    """Best-effort WebSocket notification; durable destinations belong to the relay."""
    message = report.message
    if message.conversation_id not in (None, ""):
        try:
            from app.api.websocket import manager

            event: dict[str, object] = {"type": "done", "role": "assistant", "content": message.result or ""}
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
