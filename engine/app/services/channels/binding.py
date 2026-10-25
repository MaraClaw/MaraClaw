from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.core.json_types import JsonObject
from app.core.permissions import check_agent_access
from app.core.security import load_user_principal
from app.dao import agent_dao, identity_provider_dao, user_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.company_admission_dao import company_admission_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.chat import ChatSessionRecord
from app.records.user import UserRecord
from app.schemas.chat_wire import ChatEventType
from app.services.channel_user_service import ChannelUserResolutionError, channel_user_service
from app.services.shared_chat import ensure_shared_session, is_shared_session


@dataclass(frozen=True, slots=True)
class ChannelConversation:
    transcript_session: ChatSessionRecord
    reply_session: ChatSessionRecord


async def resolve_bound_sender(
    *, agent: AgentRecord, channel_type: str, external_user_id: str | None, extra_info: JsonObject
) -> UserRecord:
    if agent.tenant_id is None:
        raise ChannelUserResolutionError("A tenant membership is required for direct messages.")
    for provider_type in channel_user_service._legacy_provider_types_for_channel(channel_type):
        provider = await identity_provider_dao.get_by_type_and_tenant(provider_type, agent.tenant_id)
        if provider is None or not provider.is_active:
            continue
        member = await channel_user_service._find_org_member(provider.id, channel_type, external_user_id, extra_info)
        if member is None or member.user_id is None or member.status != "active":
            continue
        user = await user_dao.get_with_identity(member.user_id, fresh=True)
        if user is not None and user.tenant_id == agent.tenant_id:
            return user
    raise ChannelUserResolutionError("Link your channel identity to an authorized membership in the web application.")


async def bind_channel_conversation(
    *,
    agent: AgentRecord,
    platform_user: UserRecord,
    reply_session: ChatSessionRecord,
    is_group: bool | None = None,
) -> ChannelConversation:
    group_event = reply_session.is_group if is_group is None else is_group
    if group_event:
        return ChannelConversation(reply_session, reply_session)
    principal = await load_user_principal(platform_user.id, fresh=True)
    if principal.tenant_id is None or principal.tenant_id != agent.tenant_id:
        raise ChannelUserResolutionError("The sender is not a member of this agent's tenant.")
    tenant = await company_admission_dao.tenant(principal.tenant_id)
    if tenant is None or not tenant.is_active:
        raise ChannelUserResolutionError("The sender's company is inactive.")
    await check_agent_access(principal, agent.id, fresh=True)
    transcript = await ensure_shared_session(agent.id)
    return ChannelConversation(transcript, reply_session)


async def persist_channel_user_message(
    *,
    agent_id: UUID,
    user_id: UUID,
    conversation_id: str,
    content: str,
    thinking: str | None = None,
    participant_id: UUID | None = None,
    touch_last_active: bool = False,
    agent: AgentRecord | None = None,
) -> None:
    async with connection_ctx():
        message = await chat_message_dao.insert_message(
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conversation_id,
            role="user",
            content=content,
            thinking=thinking,
            participant_id=participant_id,
        )
        session = await chat_session_dao.get(UUID(conversation_id))
        if session is not None:
            await chat_session_dao.update(db_obj=session, obj_in={"last_message_at": datetime.now(UTC)})
            if is_shared_session(session):
                from app.api.background_tasks import schedule_background_task

                schedule_background_task(
                    _publish_shared_user_message(
                        agent_id,
                        conversation_id,
                        {
                            "type": ChatEventType.DONE,
                            "role": "user",
                            "content": content,
                            "message_id": str(message.id),
                            "session_id": conversation_id,
                        },
                    ),
                    "Publish shared IM user message",
                )
        if touch_last_active:
            row = agent if agent is not None else await agent_dao.get(agent_id)
            if row is not None:
                await agent_dao.update(db_obj=row, obj_in={"last_active_at": datetime.now(UTC)})


async def _publish_shared_user_message(agent_id: UUID, conversation_id: str, event: dict[str, object]) -> None:
    from app.api.websocket import manager

    await manager.send_to_session(str(agent_id), conversation_id, event)
