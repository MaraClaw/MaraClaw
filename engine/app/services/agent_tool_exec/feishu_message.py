from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.logging import logger
from app.core.permissions import evaluate_human_relationship_status
from app.dao.agent_dao import agent_dao
from app.dao.agent_relationship_dao import agent_relationship_dao
from app.dao.channel_config_dao import channel_config_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.records.org import OrgMemberRecord
from app.services import feishu_service as feishu_service_module
from app.services.agent_tool_exec.registry import ToolArguments
from app.services.channel_session import find_or_create_channel_session
from app.services.channel_user_service import get_platform_user_by_org_member


@dataclass(frozen=True, slots=True)
class _OutgoingFeishuMessage:
    agent_id: uuid.UUID
    member: OrgMemberRecord | object
    feishu_user_id: str
    member_name: str
    message_text: str


async def _save_outgoing_to_feishu_session(outgoing: _OutgoingFeishuMessage) -> None:
    """Save an outgoing Feishu P2P message without blocking send success."""
    try:
        agent = await agent_dao.get(outgoing.agent_id)
        platform_user = await get_platform_user_by_org_member(
            db=None,
            org_member=outgoing.member,
            agent_tenant_id=agent.tenant_id if agent else None,
        )
        session = await find_or_create_channel_session(
            db=None,
            agent_id=outgoing.agent_id,
            user_id=platform_user.id,
            external_conv_id=f"feishu_p2p_{outgoing.feishu_user_id}",
            source_channel="feishu",
            first_message_title=f"[Agent → {outgoing.member_name or outgoing.feishu_user_id}]",
        )
        _ = await chat_message_dao.insert_message(
            agent_id=outgoing.agent_id,
            user_id=platform_user.id,
            role="assistant",
            content=outgoing.message_text,
            conversation_id=str(session.id),
        )
        _ = await chat_session_dao.update(db_obj=session, obj_in={"last_message_at": datetime.now(UTC)})
        logger.info(f"[Feishu] Saved outgoing message to session {session.id} (user_id: {outgoing.feishu_user_id})")
    except Exception as error:
        logger.error(f"[Feishu] Failed to save outgoing message to history: {error}")


async def _send_feishu_message(agent_id: uuid.UUID, args: ToolArguments) -> str:
    """Send a Feishu message to a person in the agent's relationship list."""
    member_name_value = args.get("member_name", "")
    direct_user_id_value = args.get("user_id", "")
    message_text_value = args.get("message", "")
    member_name = member_name_value.strip() if isinstance(member_name_value, str) else ""
    direct_user_id = direct_user_id_value.strip() if isinstance(direct_user_id_value, str) else ""
    message_text = message_text_value.strip() if isinstance(message_text_value, str) else ""

    if not message_text:
        return "❌ Please provide message content"
    if not member_name and not direct_user_id:
        return "❌ Please provide member_name or user_id"

    try:
        config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="feishu")
        if not config:
            return "❌ This agent has no Feishu channel configured"
        app_id = config.app_id
        app_secret = config.app_secret
        if not app_id or not app_secret:
            return "❌ This agent has no Feishu channel configured"

        if direct_user_id and not member_name:
            direct_relationship = await agent_relationship_dao.get_active_for_agent_by_feishu_id(
                agent_id, direct_user_id
            )
            if not direct_relationship:
                return "❌ Recipient is not in your active relationship network"
            status_info = await evaluate_human_relationship_status(None, direct_relationship)
            if status_info["access_status"] != "active":
                return (
                    "❌ Relationship to recipient is not active "
                    + f"({status_info['access_status_reason'] or 'restricted'})"
                )
            try:
                response = await feishu_service_module.feishu_service.send_message(
                    app_id,
                    app_secret,
                    receive_id=direct_user_id,
                    msg_type="text",
                    content=json.dumps({"text": message_text}, ensure_ascii=False),
                    receive_id_type="user_id",
                )
                if response.get("code") == 0:
                    await _save_outgoing_to_feishu_session(
                        _OutgoingFeishuMessage(
                            agent_id=agent_id,
                            member=direct_relationship.member,
                            feishu_user_id=direct_user_id,
                            member_name=member_name,
                            message_text=message_text,
                        ),
                    )
                    return f"✅ 消息已发送（user_id: {direct_user_id}）"
                return f"❌ 发送失败：{response.get('msg')} (code {response.get('code')})"
            except feishu_service_module.FeishuAPIError as user_id_error:
                logger.info(f"❌ 发送失败(user_id): {user_id_error.msg}")
                return f"❌ 飞书发送失败：{user_id_error.user_message}"

        relationships = await agent_relationship_dao.list_for_agent_with_members(agent_id)

        target_member = None
        for relationship in relationships:
            status_info = await evaluate_human_relationship_status(None, relationship)
            if (
                relationship.member
                and status_info["access_status"] == "active"
                and relationship.member.name == member_name
            ):
                target_member = relationship.member
                break

        if not target_member:
            logger.info(f"❌ {member_name} has no Feishu user_id in relationship")
            return f"❌ {member_name} 不是我的关系"

        logger.info(
            f"target_member={target_member.external_id}, {target_member.open_id}, "
            + f"{target_member.email}, {target_member.phone}"
        )
        if not target_member.external_id:
            logger.error(f"❌ {member_name} has no linked Feishu user_id")
            return f"❌ {member_name} 没有关联可用的飞书 user_id"

        try:
            response = await feishu_service_module.feishu_service.send_message(
                app_id,
                app_secret,
                receive_id=target_member.external_id,
                msg_type="text",
                content=json.dumps({"text": message_text}, ensure_ascii=False),
                receive_id_type="user_id",
            )
            if response.get("code") == 0:
                await _save_outgoing_to_feishu_session(
                    _OutgoingFeishuMessage(
                        agent_id=agent_id,
                        member=target_member,
                        feishu_user_id=target_member.external_id,
                        member_name=member_name,
                        message_text=message_text,
                    ),
                )
                return f"✅ Successfully sent message to {member_name}"
            logger.info(f"❌ Failed to send message to {target_member.external_id} via Feishu (user_id): {response}")
            return f"发送失败: {response.get('msg')} (code {response.get('code')})"
        except feishu_service_module.FeishuAPIError as user_id_error:
            logger.info(
                f"❌ Failed to send message to {target_member.external_id} via Feishu (user_id): {user_id_error}"
            )
            return f"❌ 飞书发送失败：{user_id_error.user_message}"
    except Exception as error:
        return f"❌ Message send error: {str(error)[:200]}"
