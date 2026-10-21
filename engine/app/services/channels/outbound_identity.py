from copy import deepcopy
from dataclasses import dataclass
from typing import assert_never

import httpx

from app.core.json_types import json_as_str_or, json_object_from_response
from app.records.outbound_plan import OutboundTarget
from app.services.channels.outbound_plan import ConfiguredOutbound
from app.services.channels.outbound_transport import require_ack


@dataclass(frozen=True, slots=True)
class OutboundPreflight:
    snapshot: ConfiguredOutbound
    target: OutboundTarget


async def preflight_outbound(outbound: ConfiguredOutbound) -> OutboundPreflight:
    snapshot = deepcopy(outbound)
    target, config = snapshot.target, snapshot.config
    account = target.account
    match target.provider:
        case "slack":
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await client.post(
                    "https://slack.com/api/auth.test", headers={"Authorization": f"Bearer {config.app_secret or ''}"}
                )
            _ = response.raise_for_status()
            body = json_object_from_response(response)
            team_id = json_as_str_or(body.get("team_id")).strip()
            bot_id = json_as_str_or(body.get("bot_id")).strip()
            require_ack(
                response, target.provider, body.get("ok") is True and bool(team_id and bot_id), "identity_unavailable"
            )
            account += (team_id, bot_id)
        case "discord":
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await client.get(
                    "https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bot {config.app_secret or ''}"}
                )
            _ = response.raise_for_status()
            body = json_object_from_response(response)
            bot_id = json_as_str_or(body.get("id")).strip()
            require_ack(response, target.provider, body.get("bot") is True and bool(bot_id), "identity_unavailable")
            account += (bot_id,)
        case "feishu" | "microsoft_teams" | "google_chat" | "wecom" | "dingtalk" | "whatsapp" | "wechat":
            pass
        case unreachable:
            assert_never(unreachable)
    return OutboundPreflight(snapshot, target.model_copy(update={"account": account}))
