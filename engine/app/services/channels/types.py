"""Canonical channel type registry for MaraClaw chat / IM integrations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class ChannelKind:
    """Descriptor for one chat / IM channel type.

    ``channel_type`` is the value stored on ``channel_configs.channel_type``
    (Postgres ``channel_type_enum``).  ``outbound_key`` is the short name used
    by ``send_channel_message`` tool routing.
    """

    channel_type: str
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
    "feishu": ChannelKind("feishu", "feishu", "Feishu", "stream"),
    "wecom": ChannelKind("wecom", "wecom", "WeCom", "stream"),
    "wechat": ChannelKind("wechat", "wechat", "WeChat", "poll"),
    "whatsapp": ChannelKind(
        "whatsapp",
        "whatsapp",
        "WhatsApp",
        "webhook",
        supports_proactive=False,  # inbound exists; proactive sender not registered yet
    ),
    "dingtalk": ChannelKind("dingtalk", "dingtalk", "DingTalk", "stream"),
    "slack": ChannelKind("slack", "slack", "Slack", "webhook"),
    "discord": ChannelKind("discord", "discord", "Discord", "gateway", supports_proactive=False),
    "microsoft_teams": ChannelKind("microsoft_teams", "teams", "MS Teams", "webhook"),
    "google_chat": ChannelKind("google_chat", "google_chat", "Google Chat", "webhook"),
    "atlassian": ChannelKind(
        "atlassian",
        "atlassian",
        "Atlassian",
        "skill_only",
        supports_inbound=False,
        supports_proactive=False,
    ),
    "agentbay": ChannelKind(
        "agentbay",
        "agentbay",
        "AgentBay",
        "skill_only",
        supports_inbound=False,
        supports_proactive=False,
    ),
}


def normalize_channel_type(value: str | None) -> str | None:
    """Normalize a free-form channel name to the stored ``channel_type``."""
    if not value:
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    if raw in CHANNEL_TYPES:
        return raw
    mapped = _ALIASES.get(raw)
    if mapped:
        return mapped
    return raw


def outbound_provider_key(value: str | None) -> str | None:
    """Normalize to the key used by proactive ``send_channel_message`` routing."""
    stored = normalize_channel_type(value)
    if not stored:
        return None
    kind = CHANNEL_TYPES.get(stored)
    if kind:
        return kind.outbound_key
    # Legacy: outbound used short "teams" while DB used microsoft_teams
    if stored == "teams":
        return "teams"
    return stored


def is_known_channel_type(value: str | None) -> bool:
    stored = normalize_channel_type(value)
    return bool(stored and stored in CHANNEL_TYPES)
