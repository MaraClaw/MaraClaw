import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import assert_never

import httpx

from app.core.json_types import JsonObject, json_as_str_or, json_object_from, json_object_from_response
from app.records.outbound_plan import OutboundChunk, OutboundProvider
from app.services import im_token_cache
from app.services.channels.outbound_errors import ProviderDeliveryError, provider_deadline, response_body
from app.services.channels.outbound_http import outbound_client
from app.services.channels.outbound_plan import ConfiguredOutbound
from app.services.channels.whatsapp_window import require_reply_window, utcnow


def require_ack(response: httpx.Response, provider: OutboundProvider, acknowledged: bool, code: str) -> None:
    if not acknowledged:
        raise ProviderDeliveryError(provider, code, provider_deadline(response, datetime.now(UTC)))


# Feishu "missing/invalid access token" business codes, the token subset of `_check_feishu_err` in
# agent_tool_exec/_agent_tool_exec_feishu_client.py (the rest of that set are permission codes).
_FEISHU_TOKEN_REJECTED = frozenset({99991661, 99991663, 99991668})


def _feishu_token_rejected(response: httpx.Response) -> bool:
    try:
        code = json_object_from_response(response).get("code")
    except ValueError:
        return False
    return isinstance(code, int) and code in _FEISHU_TOKEN_REJECTED


async def _feishu_token(app_id: str, app_secret: str, provider: OutboundProvider) -> str:
    """Shared cached app access token under the key every Feishu path uses."""
    from app.services.feishu_service import get_feishu_app_token

    try:
        return await get_feishu_app_token(app_id, app_secret)
    except im_token_cache.ImTokenRefusedError as refused:
        _ = response_body(refused.response, provider)
        retry_at = provider_deadline(refused.response, datetime.now(UTC))
        raise ProviderDeliveryError(provider, "authentication_failed", retry_at) from refused


async def _feishu_send(
    client: httpx.AsyncClient, recipient_type: str, token: str, payload: JsonObject
) -> httpx.Response:
    from app.services.feishu_service import FEISHU_SEND_MSG_URL

    return await client.post(
        FEISHU_SEND_MSG_URL,
        params={"receive_id_type": recipient_type},
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )


async def send_chunk(
    outbound: ConfiguredOutbound, chunk: OutboundChunk, *, clock: Callable[[], datetime] = utcnow
) -> str | None:
    """Send one chunk; return the provider message id only where the acknowledgement carries one (WhatsApp)."""
    target, config = outbound.target, outbound.config
    if target.mode == "personal_qr":
        from app.services.channels.whatsapp_personal_journal import enqueue

        await enqueue(target, chunk)
        return None
    app_id, app_secret = config.app_id or "", config.app_secret or ""
    provider_message_id: str | None = None
    client = outbound_client()
    match target.provider:
        case OutboundProvider.SLACK:
            response = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {app_secret}"},
                json={"channel": target.destination, "text": chunk.text},
            )
            body = response_body(response, target.provider)
            require_ack(response, target.provider, body.get("ok") is True, str(body.get("error", "missing_ack")))
        case OutboundProvider.DISCORD:
            response = await client.post(
                f"https://discord.com/api/v10/channels/{target.destination}/messages",
                headers={"Authorization": f"Bot {app_secret}"},
                json={"content": chunk.text, "nonce": chunk.key.hex[:25], "enforce_nonce": True},
            )
            body = response_body(response, target.provider)
            require_ack(response, target.provider, bool(body.get("id")), "missing_ack")
        case OutboundProvider.WHATSAPP:
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
            if isinstance(messages, list):
                provider_message_id = json_as_str_or(json_object_from(messages[0]).get("id"))
        case OutboundProvider.GOOGLE_CHAT:
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
        case OutboundProvider.FEISHU:
            if not app_id or not app_secret:
                raise ValueError("Feishu credentials are unavailable")
            feishu_payload: JsonObject = {
                "receive_id": target.destination,
                "msg_type": "text",
                "content": json.dumps({"text": chunk.text}),
                "uuid": str(chunk.key),
            }
            token = await _feishu_token(app_id, app_secret, target.provider)
            response = await _feishu_send(client, target.recipient_type, token, feishu_payload)
            if _feishu_token_rejected(response):
                # One retry with a fresh token; the payload's uuid keeps a delivered message from duplicating.
                await im_token_cache.drop_rejected_im_token(
                    im_token_cache.ImTokenKey("feishu", app_id, app_secret), token
                )
                token = await _feishu_token(app_id, app_secret, target.provider)
                response = await _feishu_send(client, target.recipient_type, token, feishu_payload)
            body = response_body(response, target.provider)
            accepted = body.get("code") == 0 and bool(json_object_from(body.get("data")).get("message_id"))
            require_ack(response, target.provider, accepted, str(body.get("code", "missing_ack")))
        case (
            OutboundProvider.MICROSOFT_TEAMS
            | OutboundProvider.WECOM
            | OutboundProvider.DINGTALK
            | OutboundProvider.WECHAT
        ):
            from app.services.channels.outbound_enterprise import send_enterprise_chunk

            await send_enterprise_chunk(client, outbound, chunk)
        case unreachable:
            assert_never(unreachable)
    return provider_message_id
