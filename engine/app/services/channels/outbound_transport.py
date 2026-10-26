import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import assert_never

import httpx

from app.core.json_types import JsonObject, json_as_str_or, json_object_from
from app.records.outbound_plan import OutboundChunk, OutboundProvider
from app.services.channels.outbound_errors import ProviderDeliveryError, provider_deadline, response_body
from app.services.channels.outbound_plan import ConfiguredOutbound
from app.services.channels.whatsapp_window import require_reply_window, utcnow


def require_ack(response: httpx.Response, provider: OutboundProvider, acknowledged: bool, code: str) -> None:
    if not acknowledged:
        raise ProviderDeliveryError(provider, code, provider_deadline(response, datetime.now(UTC)))


async def send_chunk(
    outbound: ConfiguredOutbound, chunk: OutboundChunk, *, clock: Callable[[], datetime] = utcnow
) -> None:
    target, config = outbound.target, outbound.config
    app_id, app_secret = config.app_id or "", config.app_secret or ""
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        match target.provider:
            case "slack":
                response = await client.post(
                    "https://slack.com/api/chat.postMessage",
                    headers={"Authorization": f"Bearer {app_secret}"},
                    json={"channel": target.destination, "text": chunk.text},
                )
                body = response_body(response, target.provider)
                require_ack(response, target.provider, body.get("ok") is True, str(body.get("error", "missing_ack")))
            case "discord":
                response = await client.post(
                    f"https://discord.com/api/v10/channels/{target.destination}/messages",
                    headers={"Authorization": f"Bot {app_secret}"},
                    json={"content": chunk.text, "nonce": chunk.key.hex[:25], "enforce_nonce": True},
                )
                body = response_body(response, target.provider)
                require_ack(response, target.provider, bool(body.get("id")), "missing_ack")
            case "whatsapp":
                if not app_id or not app_secret:
                    raise ValueError("WhatsApp credentials are unavailable")
                await require_reply_window(outbound, clock)
                response = await client.post(
                    f"https://graph.facebook.com/{target.endpoint}/{app_id}/messages",
                    headers={"Authorization": f"Bearer {app_secret}"},
                    json={
                        "messaging_product": "whatsapp",
                        "recipient_type": "individual",
                        "to": target.destination,
                        "type": "text",
                        "text": {"preview_url": False, "body": chunk.text},
                    },
                )
                body = response_body(response, target.provider)
                messages = body.get("messages")
                accepted = (
                    isinstance(messages, list)
                    and bool(messages)
                    and all(bool(json_object_from(message).get("id")) for message in messages)
                    and not bool(body.get("error"))
                )
                require_ack(response, target.provider, accepted, "missing_ack")
            case "google_chat":
                from app.services.channels.google_chat import (
                    CHAT_API_BASE,
                    CHAT_BOT_SCOPE,
                    _service_account_access_token,
                    _service_account_info,
                )

                info = _service_account_info(config)
                if info is None or not info:
                    raise ValueError("Google Chat service account is unavailable")
                token = await _service_account_access_token(info, scopes=[CHAT_BOT_SCOPE])
                payload: JsonObject = {"text": chunk.text}
                params = {"requestId": str(chunk.key)}
                if target.thread is not None:
                    payload["thread"] = {"name": target.thread}
                    params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
                response = await client.post(
                    f"{CHAT_API_BASE}/{target.destination}/messages",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                )
                body = response_body(response, target.provider)
                require_ack(response, target.provider, bool(body.get("name")), "missing_ack")
            case "feishu":
                from app.services.feishu_service import FEISHU_APP_ACCESS_ENDPOINT, FEISHU_SEND_MSG_URL

                if not app_id or not app_secret:
                    raise ValueError("Feishu credentials are unavailable")
                auth = await client.post(
                    FEISHU_APP_ACCESS_ENDPOINT,
                    json={"app_id": app_id, "app_secret": app_secret},
                )
                auth_body = response_body(auth, target.provider)
                token = json_as_str_or(auth_body.get("app_access_token"))
                require_ack(auth, target.provider, auth_body.get("code") == 0 and bool(token), "authentication_failed")
                response = await client.post(
                    FEISHU_SEND_MSG_URL,
                    params={"receive_id_type": target.recipient_type},
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "receive_id": target.destination,
                        "msg_type": "text",
                        "content": json.dumps({"text": chunk.text}),
                        "uuid": str(chunk.key),
                    },
                )
                body = response_body(response, target.provider)
                accepted = body.get("code") == 0 and bool(json_object_from(body.get("data")).get("message_id"))
                require_ack(response, target.provider, accepted, str(body.get("code", "missing_ack")))
            case "microsoft_teams" | "wecom" | "dingtalk" | "wechat":
                from app.services.channels.outbound_enterprise import send_enterprise_chunk

                await send_enterprise_chunk(client, outbound, chunk)
            case unreachable:
                assert_never(unreachable)
