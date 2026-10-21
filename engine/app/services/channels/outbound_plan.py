from dataclasses import dataclass
from os import environ
from typing import assert_never
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from pydantic import TypeAdapter

from app.core.json_types import json_as_str_or, json_object_from
from app.dao.channel_config_dao import channel_config_dao
from app.records.agent import AgentRecord
from app.records.channel_config import ChannelConfigRecord
from app.records.chat import ChatSessionRecord
from app.records.outbound_plan import OutboundChunk, OutboundPlan, OutboundProvider, OutboundTarget
from app.services.channels.google_chat import _service_account_info, parse_external_conv_id
from app.services.channels.types import normalize_channel_type
from app.services.wechat_channel import WECHAT_ILINK_BASE_URL

_provider = TypeAdapter(OutboundProvider)
_LIMITS: dict[OutboundProvider, int] = {
    "feishu": 20000,
    "slack": 4000,
    "microsoft_teams": 28000,
    "google_chat": 3500,
    "wecom": 2048,
    "dingtalk": 2000,
    "whatsapp": 4096,
    "wechat": 2000,
    "discord": 2000,
}


@dataclass(frozen=True, slots=True)
class ConfiguredOutbound:
    target: OutboundTarget
    config: ChannelConfigRecord


async def configured_outbound(agent: AgentRecord, session: ChatSessionRecord) -> ConfiguredOutbound:
    provider = _provider.validate_python(normalize_channel_type(session.source_channel))
    config = await channel_config_dao.get_configured_for_agent(agent.id, channel_type=provider)
    if config is None and provider == "feishu" and agent.tenant_id is not None:
        config = await channel_config_dao.get_for_tenant_channel(tenant_id=agent.tenant_id, channel_type=provider)
    if config is None:
        raise ValueError("IM configuration is unavailable")
    extra = json_object_from(config.extra_config)
    external = (session.external_conv_id or "").strip()
    destination, thread, recipient_type, endpoint, enterprise_agent_id = external, None, "", "", ""
    account: tuple[str, ...] = (config.app_id or "",)
    match provider:
        case "slack" | "discord":
            token = (config.app_secret or "").strip()
            if not token:
                raise ValueError("IM bot token is unavailable")
            if provider == "slack":
                destination = external.removeprefix("slack_").removeprefix("dm_")
            else:
                destination = external.removeprefix("discord_dm_").removeprefix("discord_").split("_", 1)[0]
        case "feishu":
            if external.startswith("feishu_group_"):
                destination, recipient_type = external.removeprefix("feishu_group_"), "chat_id"
            else:
                destination = external.removeprefix("feishu_p2p_")
                recipient_type = "open_id" if destination.startswith("ou_") else "user_id"
        case "microsoft_teams":
            from app.services.channels.teams_auth import is_bot_framework_service_url

            endpoint = json_as_str_or(extra.get("service_url")).rstrip("/")
            if not is_bot_framework_service_url(endpoint):
                raise ValueError("Teams service URL is unavailable or invalid")
            account += (
                json_as_str_or(extra.get("tenant_id")) or environ.get("TEAMS_TENANT_ID") or "common",
                str(bool(extra.get("use_managed_identity"))),
            )
        case "google_chat":
            destination, thread = parse_external_conv_id(external)
            info = _service_account_info(config)
            if info is None or not info:
                raise ValueError("Google Chat service account is unavailable")
            account = (json_as_str_or(info.get("client_email")),)
        case "wecom" | "dingtalk":
            if external.startswith(f"{provider}_group_"):
                raise ValueError("IM group destination is unsupported")
            destination = external.removeprefix(f"{provider}_p2p_")
            enterprise_agent_id = json_as_str_or(extra.get("wecom_agent_id" if provider == "wecom" else "agent_id"))
        case "whatsapp":
            destination = external.removeprefix("whatsapp_")
            endpoint = json_as_str_or(extra.get("api_version"), "v23.0")
        case "wechat":
            destination = external.removeprefix("wechat_")
            if not (config.app_id or "").strip() or not (config.app_secret or "").strip():
                raise ValueError("WeChat bot identity or token is unavailable")
            endpoint = json_as_str_or(extra.get("baseurl"), WECHAT_ILINK_BASE_URL).strip().rstrip("/")
            parsed = urlsplit(endpoint)
            if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
                raise ValueError("WeChat endpoint must be credential-free")
        case unreachable:
            assert_never(unreachable)
    if not destination or not external:
        raise ValueError("IM destination is unavailable")
    target = OutboundTarget(
        provider=provider,
        config_id=config.id,
        account=account,
        destination=destination,
        thread=thread,
        recipient_type=recipient_type,
        endpoint=endpoint,
        enterprise_agent_id=enterprise_agent_id,
    )
    return ConfiguredOutbound(target, config)


def split_chunks(content: str, provider: OutboundProvider) -> tuple[str, ...]:
    if not content.strip():
        raise ValueError("Cannot plan empty IM content")
    limit = _LIMITS[provider]
    chunks: list[str] = []
    start, size = 0, 0
    for index, character in enumerate(content):
        width = len(character.encode()) if provider in {"wecom", "microsoft_teams"} else 1
        if size + width > limit:
            chunks.append(content[start:index])
            start, size = index, 0
        size += width
    chunks.append(content[start:])
    return tuple(chunks)


def build_plan(target: OutboundTarget, content: str, delivery_id: UUID) -> OutboundPlan:
    return OutboundPlan(
        target=target,
        chunks=tuple(
            OutboundChunk(text=text, key=uuid5(delivery_id, f"v1:{ordinal}"))
            for ordinal, text in enumerate(split_chunks(content, target.provider))
        ),
    )
