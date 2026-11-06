"""Gateway rows written by Personal burst intake: the pending burst anchor and the static "not queued" source."""

from datetime import UTC, datetime
from typing import Final

from app.dao.chat_dao import chat_session_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao
from app.dao.personal_burst_dao import personal_burst_dao
from app.records.gateway_message import GatewayMessageRecord
from app.records.personal_burst_notice import BUSY_NOTICE_ROUTING_REASON
from app.services.channels.personal_burst_original import PersonalOriginal
from app.services.channels.types import CHANNEL_TYPES, normalize_channel_type
from app.services.gateway_report_wake import wake_gateway_report_relay

# Reserved marker: a batched anchor can never be mistaken for a legacy, immediately ready row.
BURST_ROUTING_REASON: Final = "whatsapp_burst"


async def _pin_reply_intent(gateway: GatewayMessageRecord, original: PersonalOriginal) -> None:
    destination = await chat_session_dao.lock_for_agent(original.reply_session_id, original.agent.id)
    channel = CHANNEL_TYPES.get(normalize_channel_type(destination.source_channel) or "") if destination else None
    if (
        destination is None
        or channel is None
        or not channel.supports_inbound
        or not (destination.external_conv_id or "").strip()
    ):
        raise ValueError("Reply destination is not a provider session for this agent")
    _ = await gateway_report_delivery_dao.create_intent(
        message_id=gateway.id, destination_kind="im", destination_id=destination.id, tenant_id=original.agent.tenant_id
    )


async def create_burst_anchor(original: PersonalOriginal) -> GatewayMessageRecord:
    """Pending FIFO-tail row for a new batch. Routing is deferred to dispatch, so no model is chosen here.

    Needs the caller's transaction and shared-session lock (``next_created_at`` enforces both).
    """
    created_at = await personal_burst_dao.next_created_at(original.agent.id, original.shared_session_id)
    gateway = await gateway_message_dao.create(
        obj_in={
            "agent_id": original.agent.id,
            "sender_user_id": original.sender.id,
            "conversation_id": str(original.shared_session_id),
            "content": original.text,
            "status": "pending",
            "routing_reason": BURST_ROUTING_REASON,
            "created_at": created_at,
        }
    )
    await _pin_reply_intent(gateway, original)
    return gateway


async def create_notice_source(original: PersonalOriginal, result: str) -> GatewayMessageRecord:
    """Completed, conversation-less machine row carrying only the static notice; the relay delivers it."""
    gateway = await gateway_message_dao.create(
        obj_in={
            "agent_id": original.agent.id,
            "content": "",
            "status": "completed",
            "result": result,
            "routing_reason": BUSY_NOTICE_ROUTING_REASON,
            "completed_at": datetime.now(UTC),
        }
    )
    await _pin_reply_intent(gateway, original)
    wake_gateway_report_relay()
    return gateway
