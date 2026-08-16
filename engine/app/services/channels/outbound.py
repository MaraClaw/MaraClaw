"""Deliver a guest report back to the originating IM conversation."""

from __future__ import annotations

from typing import Any

from app.core.json_types import json_as_str, json_object_from
from app.core.logging import logger
from app.dao.channel_config_dao import channel_config_dao
from app.records.agent import AgentRecord
from app.records.chat import ChatSessionRecord
from app.services.channels.types import normalize_channel_type

WEB_OR_AGENT_CHANNELS = frozenset({"web", "agent", ""})


def _split_conv(external_conv_id: str, prefix: str) -> str:
    if external_conv_id.startswith(prefix):
        return external_conv_id[len(prefix) :]
    return external_conv_id


async def deliver_session_reply(*, agent: AgentRecord, session: ChatSessionRecord, content: str) -> None:
    """Send ``content`` to the IM thread that owns ``session``. No-op for web/A2A."""
    if not content.strip():
        return
    channel = normalize_channel_type(session.source_channel) or (session.source_channel or "")
    if channel in WEB_OR_AGENT_CHANNELS:
        return
    external = (session.external_conv_id or "").strip()
    if not external:
        logger.warning("[Outbound] session %s has no external_conv_id (channel=%s)", session.id, channel)
        return
    try:
        if channel == "feishu":
            await _deliver_feishu(agent, external, content)
        elif channel == "slack":
            await _deliver_slack(agent, external, content)
        elif channel in {"microsoft_teams", "teams"}:
            await _deliver_teams(agent, external, content)
        elif channel == "google_chat":
            await _deliver_google_chat(agent, external, content)
        elif channel == "wecom":
            await _deliver_wecom(agent, external, content)
        elif channel == "dingtalk":
            await _deliver_dingtalk(agent, external, content)
        elif channel == "whatsapp":
            await _deliver_whatsapp(agent, external, content)
        elif channel == "wechat":
            await _deliver_wechat(agent, external, content)
        elif channel == "discord":
            await _deliver_discord(agent, external, content)
        else:
            logger.info("[Outbound] no IM delivery for channel=%s session=%s", channel, session.id)
    except Exception:
        logger.exception("[Outbound] failed channel=%s session=%s", channel, session.id)


async def _deliver_feishu(agent: AgentRecord, external: str, content: str) -> None:
    from app.services.feishu_service import feishu_service

    config = await channel_config_dao.get_configured_for_agent(agent.id, channel_type="feishu")
    if config is None and agent.tenant_id:
        config = await channel_config_dao.get_for_tenant_channel(tenant_id=agent.tenant_id, channel_type="feishu")
    if config is None or not config.app_id or not config.app_secret:
        logger.warning("[Outbound] no Feishu config for agent %s", agent.id)
        return
    if external.startswith("feishu_group_"):
        receive_id, receive_id_type = _split_conv(external, "feishu_group_"), "chat_id"
    else:
        receive_id, receive_id_type = _split_conv(external, "feishu_p2p_"), "open_id"
        if receive_id and not receive_id.startswith("ou_"):
            receive_id_type = "user_id"
    import json

    _ = await feishu_service.send_message(
        config.app_id,
        config.app_secret,
        receive_id,
        "text",
        json.dumps({"text": content}),
        receive_id_type=receive_id_type,
        stage="gateway_report",
    )


async def _deliver_slack(agent: AgentRecord, external: str, content: str) -> None:
    from app.api.slack import _send_slack_messages

    config = await channel_config_dao.get_configured_for_agent(agent.id, channel_type="slack")
    if config is None or not (config.app_secret or "").strip():
        logger.warning("[Outbound] no Slack config for agent %s", agent.id)
        return
    channel_id = _split_conv(external, "slack_")
    if channel_id.startswith("dm_"):
        channel_id = channel_id[3:]
    await _send_slack_messages(config.app_secret or "", channel_id, content)


async def _deliver_teams(agent: AgentRecord, external: str, content: str) -> None:
    from app.api.teams import _send_teams_message

    config = await channel_config_dao.get_configured_for_agent(agent.id, channel_type="microsoft_teams")
    if config is None:
        logger.warning("[Outbound] no Teams config for agent %s", agent.id)
        return
    activity: dict[str, Any] = {
        "type": "message",
        "text": content,
        "conversation": {"id": external},
    }
    await _send_teams_message(config, external, activity)


async def _deliver_google_chat(agent: AgentRecord, external: str, content: str) -> None:
    from app.services.channels.google_chat import parse_external_conv_id, send_google_chat_message

    config = await channel_config_dao.get_configured_for_agent(agent.id, channel_type="google_chat")
    if config is None:
        logger.warning("[Outbound] no Google Chat config for agent %s", agent.id)
        return
    space_name, thread_name = parse_external_conv_id(external)
    if not space_name:
        logger.warning("[Outbound] cannot parse Google Chat conv %s", external)
        return
    _ = await send_google_chat_message(config, space_name=space_name, text=content, thread_name=thread_name)


async def _deliver_wecom(agent: AgentRecord, external: str, content: str) -> None:
    from app.services.wecom_service import send_wecom_message

    config = await channel_config_dao.get_configured_for_agent(agent.id, channel_type="wecom")
    if config is None or not config.app_id or not config.app_secret:
        logger.warning("[Outbound] no WeCom config for agent %s", agent.id)
        return
    extra = json_object_from(config.extra_config)
    wecom_agent_id = json_as_str(extra.get("wecom_agent_id")) or ""
    user_id = (
        _split_conv(external, "wecom_group_") if external.startswith("wecom_group_") else _split_conv(external, "wecom_p2p_")
    )
    result = await send_wecom_message(
        corp_id=config.app_id,
        secret=config.app_secret,
        user_id=user_id,
        message=content,
        agent_id=wecom_agent_id,
    )
    if result.get("errcode") not in (0, None):
        logger.warning("[Outbound] WeCom send failed: %s", result)


async def _deliver_dingtalk(agent: AgentRecord, external: str, content: str) -> None:
    from app.services.dingtalk_service import send_dingtalk_message

    config = await channel_config_dao.get_configured_for_agent(agent.id, channel_type="dingtalk")
    if config is None or not config.app_id or not config.app_secret:
        logger.warning("[Outbound] no DingTalk config for agent %s", agent.id)
        return
    extra = json_object_from(config.extra_config)
    dingtalk_agent_id = json_as_str(extra.get("agent_id")) or ""
    if external.startswith("dingtalk_group_"):
        user_id = _split_conv(external, "dingtalk_group_")
    else:
        user_id = _split_conv(external, "dingtalk_p2p_")
    result = await send_dingtalk_message(
        app_id=config.app_id,
        app_secret=config.app_secret,
        user_id=user_id,
        message=content,
        agent_id=dingtalk_agent_id,
    )
    if result.get("errcode") not in (0, None):
        logger.warning("[Outbound] DingTalk send failed: %s", result)


async def _deliver_whatsapp(agent: AgentRecord, external: str, content: str) -> None:
    from app.api.whatsapp import _send_whatsapp_messages

    config = await channel_config_dao.get_configured_for_agent(agent.id, channel_type="whatsapp")
    if config is None:
        logger.warning("[Outbound] no WhatsApp config for agent %s", agent.id)
        return
    phone = _split_conv(external, "whatsapp_")
    await _send_whatsapp_messages(config, phone, content)


async def _deliver_wechat(agent: AgentRecord, external: str, content: str) -> None:
    from app.services.wechat_channel import WECHAT_ILINK_BASE_URL, get_wechat_context_entry, send_wechat_text_message

    config = await channel_config_dao.get_configured_for_agent(agent.id, channel_type="wechat")
    if config is None:
        logger.warning("[Outbound] no WeChat config for agent %s", agent.id)
        return
    extra = json_object_from(config.extra_config)
    token = (json_as_str(extra.get("bot_token")) or "").strip()
    if not token:
        logger.warning("[Outbound] WeChat bot token missing for agent %s", agent.id)
        return
    user_id = _split_conv(external, "wechat_")
    context_entry = json_object_from(get_wechat_context_entry(config.extra_config, from_user_id=user_id))
    context_token = (json_as_str(context_entry.get("context_token")) or "").strip()
    if not context_token:
        logger.warning("[Outbound] WeChat context missing for %s", user_id)
        return
    await send_wechat_text_message(
        token=token,
        base_url=(json_as_str(extra.get("baseurl")) or WECHAT_ILINK_BASE_URL).strip(),
        to_user_id=user_id,
        context_token=context_token,
        text=content,
        route_tag=(json_as_str(extra.get("route_tag")) or "").strip() or None,
    )


async def _deliver_discord(agent: AgentRecord, external: str, content: str) -> None:
    import httpx

    config = await channel_config_dao.get_configured_for_agent(agent.id, channel_type="discord")
    if config is None or not (config.app_secret or "").strip():
        logger.warning("[Outbound] no Discord config for agent %s", agent.id)
        return
    channel_id = external
    if external.startswith("discord_dm_"):
        channel_id = _split_conv(external, "discord_dm_")
    elif external.startswith("discord_"):
        rest = _split_conv(external, "discord_")
        channel_id = rest.split("_", 1)[0]
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bot {config.app_secret}", "Content-Type": "application/json"},
            json={"content": content[:2000]},
        )
        if resp.status_code >= 400:
            logger.warning("[Outbound] Discord send failed %s: %s", resp.status_code, resp.text[:200])
