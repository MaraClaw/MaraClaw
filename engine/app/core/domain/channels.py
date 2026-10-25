"""Persisted channel configuration types, not provider registry keys."""

from enum import StrEnum


class ChannelType(StrEnum):
    FEISHU = "feishu"
    WECOM = "wecom"
    WECHAT = "wechat"
    WHATSAPP = "whatsapp"
    DINGTALK = "dingtalk"
    SLACK = "slack"
    DISCORD = "discord"
    ATLASSIAN = "atlassian"
    MICROSOFT_TEAMS = "microsoft_teams"
    AGENTBAY = "agentbay"
    GOOGLE_CHAT = "google_chat"
