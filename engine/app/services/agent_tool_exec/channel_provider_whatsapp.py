"""Guarded proactive WhatsApp follow-up for ``send_channel_message``.

Reaches only a linked contact through their existing non-group reply session, and only while the local receipt
evidence keeps the 24-hour reply window open (checked by the strict transport). A provider acknowledgement means
accepted, not delivered, and nothing here retries or resends.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from fastapi import HTTPException

from app.core.logging import logger
from app.core.permissions import compute_agent_access_level
from app.core.row_memo import memo_drop
from app.core.security import load_user_principal
from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.company_admission_dao import company_admission_dao
from app.dao.identity_provider_dao import identity_provider_dao
from app.db.session import connection_ctx
from app.records.chat import ChatSessionRecord
from app.records.org import OrgMemberRecord
from app.services.channels.outbound_errors import ProviderDeliveryError
from app.services.channels.outbound_plan import ConfiguredOutbound, build_plan, configured_outbound
from app.services.channels.outbound_transport import send_chunk
from app.services.channels.types import normalize_channel_type
from app.services.channels.whatsapp_window import WhatsAppReplyWindowError

# Cloud API text body limit; a longer plan would be split into several independently accepted messages.
_TEXT_LIMIT = 4096


class _UnavailableError(Exception):
    """A pre-send guard failed, so no request reached WhatsApp."""


@dataclass(frozen=True, slots=True)
class _Reply:
    outbound: ConfiguredOutbound
    session: ChatSessionRecord


def _not_linked(member: OrgMemberRecord) -> _UnavailableError:
    return _UnavailableError(f"{member.name} is not a linked WhatsApp contact of this company")


async def _resolve_reply(agent_id: uuid.UUID, member: OrgMemberRecord) -> _Reply:
    """Resolve the bound recipient's exact existing reply session; never create, rebind or fall back."""
    memo_drop("agent", agent_id)
    agent = await agent_dao.get(agent_id)
    if agent is None or agent.tenant_id is None:
        raise _UnavailableError("This agent is not part of a company that can use WhatsApp")
    if member.status != "active" or member.tenant_id != agent.tenant_id or member.user_id is None:
        raise _not_linked(member)
    provider = await identity_provider_dao.get_by_type_and_tenant("whatsapp", agent.tenant_id)
    if provider is None or not provider.is_active or provider.id != member.provider_id:
        raise _not_linked(member)
    try:
        recipient = await load_user_principal(member.user_id, fresh=True)
    except HTTPException:
        raise _UnavailableError(f"{member.name} is not an active member of this company") from None
    tenant = await company_admission_dao.tenant(agent.tenant_id)
    if recipient.tenant_id != agent.tenant_id or tenant is None or not tenant.is_active:
        raise _UnavailableError(f"{member.name} is not an active member of this company")
    if await compute_agent_access_level(recipient, agent) not in {"manage", "use"}:
        raise _UnavailableError(f"{member.name} is not authorized to use this agent")
    external = f"whatsapp_{(member.external_id or '').strip()}"
    session = await chat_session_dao.get_by_external_conv(agent_id=agent.id, external_conv_id=external)
    if (
        session is None
        or session.external_conv_id != external
        or session.is_group
        or session.user_id != recipient.id
        or normalize_channel_type(session.source_channel) != "whatsapp"
    ):
        raise _UnavailableError(
            f"{member.name} has no existing WhatsApp conversation with this agent; "
            "WhatsApp can only follow up after the contact has messaged"
        )
    outbound = await configured_outbound(agent, session)
    config = outbound.config
    if outbound.target.mode != "cloud" or not (config.app_id or "").strip() or not (config.app_secret or "").strip():
        raise _UnavailableError("This agent has no usable WhatsApp Cloud API channel")
    return _Reply(outbound, session)


def _uncertain(member_name: str, cause: str) -> str:
    return (
        f"⚠️ The WhatsApp message to {member_name} has an uncertain outcome: {cause}. "
        "It may or may not have reached WhatsApp, was not saved to the conversation history and was not retried. "
        f"Do not resend unless {member_name} confirms it did not arrive."
    )


def _send_failure(member_name: str, error: Exception) -> str:
    match error:
        case WhatsAppReplyWindowError(reason="whatsapp_window_expired"):
            return f"❌ {member_name}'s 24-hour WhatsApp reply window has closed; nothing was sent"
        case WhatsAppReplyWindowError():
            return f"❌ No recent WhatsApp message from {member_name} is on record; nothing was sent"
        case httpx.HTTPStatusError() if error.response.status_code < 500:
            return f"❌ WhatsApp rejected the message to {member_name} (HTTP {error.response.status_code})"
        case httpx.TimeoutException():
            return _uncertain(member_name, "the request to WhatsApp timed out")
        case ProviderDeliveryError():
            return _uncertain(member_name, "WhatsApp did not return a message ID")
        case _:
            return _uncertain(member_name, "the request to WhatsApp failed after it may have been sent")


async def _record_assistant_message(session: ChatSessionRecord, text: str) -> None:
    async with connection_ctx():
        _ = await chat_message_dao.insert_message(
            agent_id=session.agent_id,
            user_id=session.user_id,
            role="assistant",
            content=text,
            conversation_id=str(session.id),
        )
        _ = await chat_session_dao.update(db_obj=session, obj_in={"last_message_at": datetime.now(UTC)})


async def send_whatsapp_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: OrgMemberRecord,
) -> str:
    """Send one plain-text WhatsApp follow-up; report acceptance, never delivery."""
    if not message_text.strip():
        return "❌ Please provide message content"
    if len(message_text) > _TEXT_LIMIT:
        return f"❌ WhatsApp messages are limited to {_TEXT_LIMIT} characters ({len(message_text)} given)"
    try:
        reply = await _resolve_reply(agent_id, target_member)
        plan = build_plan(reply.outbound.target, message_text, uuid.uuid4())
    except _UnavailableError as error:
        return f"❌ {error}"
    except Exception:
        logger.exception("[WhatsApp] Proactive message could not be prepared")
        return "❌ The WhatsApp message could not be prepared; nothing was sent"

    try:
        message_id = await send_chunk(reply.outbound, plan.chunks[0])
    except Exception as error:
        logger.warning(f"[WhatsApp] Proactive send to session {reply.session.id} failed: {type(error).__name__}")
        return _send_failure(member_name, error)
    if message_id in (None, ""):
        return _uncertain(member_name, "WhatsApp did not return a message ID")

    try:
        await _record_assistant_message(reply.session, message_text)
    except Exception:
        logger.exception(f"[WhatsApp] Accepted message {message_id} was not saved to session {reply.session.id}")
        return (
            f"⚠️ WhatsApp accepted the message to {member_name} (message ID {message_id}) but it could not be saved "
            "to the conversation history. Do not resend it; acceptance is not a delivery confirmation."
        )
    logger.info(f"[WhatsApp] Proactive message {message_id} accepted for session {reply.session.id}")
    return (
        f"✅ WhatsApp accepted the message to {member_name} (message ID {message_id}). "
        "Acceptance is not a delivery or read confirmation."
    )
