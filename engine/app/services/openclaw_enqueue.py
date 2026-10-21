from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from app.core.logging import logger
from app.dao.chat_dao import chat_session_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao
from app.records.gateway_message import GatewayMessageRecord
from app.services.channels.types import CHANNEL_TYPES, normalize_channel_type
from app.services.shared_chat import shared_session_id

if TYPE_CHECKING:
    from app.services.openclaw_routing import PreparedOpenClawMessage


async def insert_prepared_message(
    prepared: PreparedOpenClawMessage,
    *,
    sender_user_id: UUID | None,
    sender_agent_id: UUID | None,
    conversation_id: str | None,
    deduplicate: bool = True,
    reply_session_id: UUID | None = None,
) -> GatewayMessageRecord:
    agent, content = prepared.agent, prepared.content
    shared = conversation_id == str(shared_session_id(agent.id))
    pending = (
        await gateway_message_dao.list_pending(agent.id)
        if deduplicate and not shared and sender_user_id is not None
        else ()
    )
    duplicate = next(
        (
            row
            for row in pending
            if (
                row.content == content
                and row.sender_user_id == sender_user_id
                and sender_user_id is not None
                and (row.conversation_id or "") == (conversation_id or "")
            )
        ),
        None,
    )
    if duplicate is not None:
        logger.info("[OpenClaw] skip duplicate pending inbox item agent={} id={}", agent.id, duplicate.id)
        return duplicate
    created = await gateway_message_dao.create(
        obj_in={
            "agent_id": agent.id,
            "sender_user_id": sender_user_id,
            "sender_agent_id": sender_agent_id,
            "conversation_id": conversation_id,
            "content": content,
            "status": "pending",
            "selected_slot": prepared.selected_slot,
            "guest_model_ref": prepared.guest_model_ref,
            "complexity": prepared.complexity,
            "routing_reason": prepared.routing_reason,
        }
    )
    if reply_session_id is not None:
        destination = await chat_session_dao.lock_for_agent(reply_session_id, agent.id)
        channel = CHANNEL_TYPES.get(normalize_channel_type(destination.source_channel) or "") if destination else None
        if (
            destination is None
            or channel is None
            or not channel.supports_inbound
            or not (destination.external_conv_id or "").strip()
        ):
            raise ValueError("Reply destination is not a provider session for this agent")
        _ = await gateway_report_delivery_dao.create_intent(
            message_id=created.id, destination_kind="im", destination_id=destination.id, tenant_id=agent.tenant_id
        )
    logger.info(
        "[OpenClaw] queued agent={} slot={} reason={} guest_model={}",
        agent.id,
        prepared.selected_slot,
        prepared.routing_reason,
        prepared.guest_model_ref,
    )
    return created
