"""Canonical channel type registry for MaraClaw chat / IM integrations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.core.domain.channels import ChannelType


@dataclass(frozen=True, slots=True)
class ChannelKind:
    """Descriptor for one chat / IM channel type.

    ``channel_type`` is the value stored on ``channel_configs.channel_type``
    (Postgres ``channel_type_enum``).  ``outbound_key`` is the short name used
    by ``send_channel_message`` tool routing.
    """

    channel_type: ChannelType
    outbound_key: str
    display_name: str
    transport: str  # webhook | stream | gateway | poll | skill_only
    supports_inbound: bool = True
    supports_proactive: bool = True


# Aliases map alternate / legacy names onto the stored channel_type.
_ALIASES: Final[dict[str, str]] = {
    "teams": "microsoft_teams",
    "ms_teams": "microsoft_teams",
    "msteams": "microsoft_teams",
    "googlechat": "google_chat",
    "google-chat": "google_chat",
    "gchat": "google_chat",
}


CHANNEL_TYPES: Final[dict[str, ChannelKind]] = {
    "feishu": ChannelKind(ChannelType.FEISHU, "feishu", "Feishu", "stream"),
    "wecom": ChannelKind(ChannelType.WECOM, "wecom", "WeCom", "stream"),
    "wechat": ChannelKind(ChannelType.WECHAT, "wechat", "WeChat", "poll"),
    "whatsapp": ChannelKind(
        ChannelType.WHATSAPP,
        "whatsapp",
        "WhatsApp",
        "webhook",
        supports_proactive=False,  # inbound exists; proactive sender not registered yet
    ),
    "dingtalk": ChannelKind(ChannelType.DINGTALK, "dingtalk", "DingTalk", "stream"),
    "slack": ChannelKind(ChannelType.SLACK, "slack", "Slack", "webhook"),
    "discord": ChannelKind(ChannelType.DISCORD, "discord", "Discord", "gateway", supports_proactive=False),
    "microsoft_teams": ChannelKind(ChannelType.MICROSOFT_TEAMS, "teams", "MS Teams", "webhook"),
    "google_chat": ChannelKind(ChannelType.GOOGLE_CHAT, "google_chat", "Google Chat", "webhook"),
    "atlassian": ChannelKind(
        ChannelType.ATLASSIAN,
        "atlassian",
        "Atlassian",
        "skill_only",
        supports_inbound=False,
        supports_proactive=False,
    ),
    "agentbay": ChannelKind(
        ChannelType.AGENTBAY,
        "agentbay",
        "AgentBay",
        "skill_only",
        supports_inbound=False,
        supports_proactive=False,
    ),
}


def normalize_channel_type(value: str | None) -> str | None:
    """Normalize a free-form channel name to the stored ``channel_type``."""
    if value in (None, ""):
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    if raw in CHANNEL_TYPES:
        return raw
    mapped = _ALIASES.get(raw)
    if mapped not in (None, ""):
        return mapped
    return raw


def outbound_provider_key(value: str | None) -> str | None:
    """Normalize to the key used by proactive ``send_channel_message`` routing."""
    stored = normalize_channel_type(value)
    if stored in (None, ""):
        return None
    kind = CHANNEL_TYPES.get(stored)
    if kind:
        return kind.outbound_key
    return stored


def is_known_channel_type(value: str | None) -> bool:
    stored = normalize_channel_type(value)
    return bool(stored and stored in CHANNEL_TYPES)
