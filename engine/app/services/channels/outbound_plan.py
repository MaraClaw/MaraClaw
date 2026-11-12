from dataclasses import dataclass
from os import environ
from typing import assert_never
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from pydantic import TypeAdapter

from app.core.domain.channels import ChannelType
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
    OutboundProvider.FEISHU: 20000,
    OutboundProvider.SLACK: 4000,
    OutboundProvider.MICROSOFT_TEAMS: 28000,
    OutboundProvider.GOOGLE_CHAT: 3500,
    OutboundProvider.WECOM: 2048,
    OutboundProvider.DINGTALK: 2000,
    OutboundProvider.WHATSAPP: 4096,
    OutboundProvider.WECHAT: 2000,
    OutboundProvider.DISCORD: 2000,
}


@dataclass(frozen=True, slots=True)
class ConfiguredOutbound:
    target: OutboundTarget
    config: ChannelConfigRecord
    tenant_id: UUID | None = None


async def configured_outbound(agent: AgentRecord, session: ChatSessionRecord) -> ConfiguredOutbound:
    provider = _provider.validate_python(normalize_channel_type(session.source_channel))
    channel_type = ChannelType(provider)
    config = await channel_config_dao.get_configured_for_agent(agent.id, channel_type=channel_type)
    if config is None and provider == OutboundProvider.FEISHU and agent.tenant_id is not None:
        config = await channel_config_dao.get_for_tenant_channel(tenant_id=agent.tenant_id, channel_type=channel_type)
    external = (session.external_conv_id or "").strip()
    if config is None:
        if provider == OutboundProvider.WHATSAPP and external.startswith("whatsapp_personal:"):
            from app.services.channels.whatsapp_personal_journal import PersonalSendTerminalError

            raise PersonalSendTerminalError("personal_route_revoked")
        raise ValueError("IM configuration is unavailable")
    extra = json_object_from(config.extra_config)
    if provider == OutboundProvider.WHATSAPP:
        from app.dao.whatsapp_personal_dao import get_personal
        from app.records.whatsapp_personal import PersonalRoute
        from app.services.channels.whatsapp_personal_journal import PersonalSendTerminalError, route_matches

        if external.startswith("whatsapp_personal:"):
            try:
                route = PersonalRoute.from_external_id(external)
            except ValueError:
                raise PersonalSendTerminalError("personal_route_invalid") from None
            connection = await get_personal(agent.id)
            if extra.get("mode") != "personal_qr" or connection is None or not route_matches(connection, route):
                raise PersonalSendTerminalError("personal_route_revoked")
            return ConfiguredOutbound(
                OutboundTarget(
                    provider=OutboundProvider.WHATSAPP,
                    mode="personal_qr",
                    config_id=config.id,
                    account=(route.account_id, str(route.auth_epoch), str(route.generation)),
                    destination=external,
                ),
                config,
                agent.tenant_id,
            )
        if extra.get("mode") == "personal_qr":
            raise PersonalSendTerminalError("personal_cloud_route_forbidden")
    destination, thread, recipient_type, endpoint, enterprise_agent_id = external, None, "", "", ""
    account: tuple[str, ...] = (config.app_id or "",)
    match provider:
        case OutboundProvider.SLACK | OutboundProvider.DISCORD:
            token = (config.app_secret or "").strip()
            if not token:
                raise ValueError("IM bot token is unavailable")
            if provider == OutboundProvider.SLACK:
                destination = external.removeprefix("slack_").removeprefix("dm_")
            else:
                destination = external.removeprefix("discord_dm_").removeprefix("discord_").split("_", 1)[0]
        case OutboundProvider.FEISHU:
            if external.startswith("feishu_group_"):
                destination, recipient_type = external.removeprefix("feishu_group_"), "chat_id"
            else:
                destination = external.removeprefix("feishu_p2p_")
                recipient_type = "open_id" if destination.startswith("ou_") else "user_id"
        case OutboundProvider.MICROSOFT_TEAMS:
            from app.services.channels.teams_auth import is_bot_framework_service_url

            endpoint = json_as_str_or(extra.get("service_url")).rstrip("/")
            if not is_bot_framework_service_url(endpoint):
                raise ValueError("Teams service URL is unavailable or invalid")
            account += (
                json_as_str_or(extra.get("tenant_id")) or environ.get("TEAMS_TENANT_ID") or "common",
                str(bool(extra.get("use_managed_identity"))),
            )
        case OutboundProvider.GOOGLE_CHAT:
            destination, thread = parse_external_conv_id(external)
            info = _service_account_info(config)
            if info is None or not info:
                raise ValueError("Google Chat service account is unavailable")
            account = (json_as_str_or(info.get("client_email")),)
        case OutboundProvider.WECOM | OutboundProvider.DINGTALK:
            if external.startswith(f"{provider}_group_"):
                raise ValueError("IM group destination is unsupported")
            destination = external.removeprefix(f"{provider}_p2p_")
            enterprise_agent_id = json_as_str_or(
                extra.get("wecom_agent_id" if provider == OutboundProvider.WECOM else "agent_id")
            )
        case OutboundProvider.WHATSAPP:
            destination = external.removeprefix("whatsapp_")
            endpoint = json_as_str_or(extra.get("api_version"), "v23.0")
        case OutboundProvider.WECHAT:
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
    return ConfiguredOutbound(target, config, agent.tenant_id)


def split_chunks(content: str, provider: OutboundProvider) -> tuple[str, ...]:
    if not content.strip():
        raise ValueError("Cannot plan empty IM content")
    limit = _LIMITS[provider]
    chunks: list[str] = []
    start, size = 0, 0
    for index, character in enumerate(content):
        width = len(character.encode()) if provider in {OutboundProvider.WECOM, OutboundProvider.MICROSOFT_TEAMS} else 1
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
