from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, override
from uuid import UUID

from fastapi import HTTPException

from app.core.json_types import JsonObject
from app.core.permissions import compute_agent_access_level
from app.core.row_memo import clear_entity_memo
from app.core.security import load_user_principal
from app.dao import agent_dao, channel_config_dao
from app.dao.channel_event_receipt_dao import channel_event_receipt_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.company_admission_dao import company_admission_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.channel_event_receipt import ChannelEventReceiptRecord
from app.records.user import UserRecord
from app.services.channel_session import find_or_create_channel_session
from app.services.channels.binding import bind_channel_conversation, persist_channel_user_message, resolve_bound_sender
from app.services.channels.types import normalize_channel_type
from app.services.llm.utils import convert_chat_messages_to_llm_format
from app.services.openclaw_routing import enqueue_prepared_openclaw_message, prepare_openclaw_message
from app.services.quota_guard import charge_conversation_quota, check_agent_expired


@dataclass(frozen=True, slots=True)
class ChannelEvent:
    config_id: UUID
    agent_id: UUID
    provider: str
    event_key: str
    sender_id: str
    conversation_id: str
    text: str
    kind: str = "text"
    media_ids: tuple[str, ...] = ()
    is_group: bool = False
    sender_external_id: str = ""
    sender_open_id: str = ""
    group_session_owner: Literal["creator", "sender"] = "creator"

    def fingerprint(self) -> str:
        canonical = json.dumps(
            [self.sender_id, self.conversation_id, self.is_group, self.kind, self.text, self.media_ids],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return "v1:" + hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(slots=True)
class ChannelEventConflictError(Exception):
    config_id: UUID
    provider: str
    event_key: str

    @override
    def __str__(self) -> str:
        return f"Conflicting native event {self.provider}:{self.event_key} for channel {self.config_id}"


@dataclass(frozen=True, slots=True)
class AcceptedChannelEvent:
    replayed: bool


async def _authorize(event: ChannelEvent) -> tuple[AgentRecord, UserRecord]:
    clear_entity_memo()
    config = await channel_config_dao.get(event.config_id)
    if (
        config is None
        or not config.is_configured
        or config.agent_id != event.agent_id
        or normalize_channel_type(config.channel_type) != normalize_channel_type(event.provider)
    ):
        raise HTTPException(403, "Channel configuration is unavailable")
    agent = await agent_dao.get(event.agent_id)
    if agent is None or agent.tenant_id is None:
        raise HTTPException(403, "Agent is unavailable")
    extra: JsonObject = {"external_id": event.sender_external_id or event.sender_id}
    if event.sender_open_id:
        extra["open_id"] = event.sender_open_id
    sender = await resolve_bound_sender(
        agent=agent, channel_type=event.provider, external_user_id=event.sender_id, extra_info=extra
    )
    principal = await load_user_principal(sender.id, fresh=True)
    if principal.tenant_id != agent.tenant_id:
        raise HTTPException(403, "Sender is outside the agent's tenant")
    tenant = await company_admission_dao.tenant(agent.tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(403, "Sender's company is inactive")
    if await compute_agent_access_level(principal, agent) not in {"manage", "use"}:
        raise HTTPException(403, "No access to this agent")
    return agent, principal


def _receipt(event: ChannelEvent, agent: AgentRecord) -> ChannelEventReceiptRecord:
    return ChannelEventReceiptRecord(
        config_id=event.config_id,
        provider=event.provider,
        event_key=event.event_key,
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        fingerprint=event.fingerprint(),
    )


def _matching(existing: ChannelEventReceiptRecord | None, candidate: ChannelEventReceiptRecord) -> bool:
    if existing is None:
        return False
    if (existing.agent_id, existing.tenant_id, existing.fingerprint) != (
        candidate.agent_id,
        candidate.tenant_id,
        candidate.fingerprint,
    ):
        raise ChannelEventConflictError(candidate.config_id, candidate.provider, candidate.event_key)
    return True


async def is_channel_event_replay(event: ChannelEvent) -> bool:
    agent, _ = await _authorize(event)
    candidate = _receipt(event, agent)
    return _matching(await channel_event_receipt_dao.find(candidate), candidate)


async def accept_channel_event(
    event: ChannelEvent, *, content: str | None = None, saved_content: str | None = None
) -> AcceptedChannelEvent:
    async with connection_ctx():
        agent, sender = await _authorize(event)
        candidate = _receipt(event, agent)
        if not await channel_event_receipt_dao.reserve(candidate):
            if _matching(await channel_event_receipt_dao.find(candidate), candidate):
                return AcceptedChannelEvent(replayed=True)
            raise ChannelEventConflictError(event.config_id, event.provider, event.event_key)
        await check_agent_expired(agent.id)
        await channel_event_receipt_dao.lock_conversation(candidate, event.conversation_id)
        reply = await find_or_create_channel_session(
            agent_id=agent.id,
            user_id=agent.creator_id if event.is_group and event.group_session_owner == "creator" else sender.id,
            external_conv_id=event.conversation_id,
            source_channel=normalize_channel_type(event.provider) or event.provider,
            first_message_title=event.text,
            is_group=event.is_group,
        )
        conversation = await bind_channel_conversation(
            agent=agent, platform_user=sender, reply_session=reply, is_group=event.is_group
        )
        session = await chat_session_dao.lock_for_agent(conversation.transcript_session.id, agent.id)
        if session is None:
            raise HTTPException(403, "Session is unavailable")
        await charge_conversation_quota(sender)
        history = convert_chat_messages_to_llm_format(
            await chat_message_dao.list_recent(agent_id=agent.id, conversation_id=str(session.id), limit=8)
        )
        prepared = await prepare_openclaw_message(
            agent=agent, content=event.text if content is None else content, history=history
        )
        await persist_channel_user_message(
            agent_id=agent.id,
            user_id=sender.id,
            conversation_id=str(session.id),
            content=event.text if saved_content is None else saved_content,
        )
        await enqueue_prepared_openclaw_message(
            prepared,
            sender_user_id=sender.id,
            conversation_id=str(session.id),
            reply_session_id=reply.id,
            deduplicate=False,
        )
    return AcceptedChannelEvent(replayed=False)
