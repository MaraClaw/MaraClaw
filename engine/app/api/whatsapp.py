"""WhatsApp Cloud API channel routes."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import TypedDict

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.core.domain.channels import ChannelType
from app.core.json_types import (
    JsonObject,
    json_as_str,
    json_as_str_or,
    json_loads_object,
    json_object_from,
    json_object_from_response,
)
from app.core.logging import logger
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.request_body import CHANNEL_WEBHOOK_MAX_BODY_SIZE, read_bounded_body
from app.core.security import get_current_user
from app.dao.channel_config_dao import channel_config_dao
from app.records.channel_config import ChannelConfigRecord
from app.records.user import UserRecord
from app.schemas.channel_response_contracts import WebhookUrlResponse
from app.schemas.content_response_catalog import OkAcknowledgement
from app.schemas.schemas import ChannelConfigOut
from app.services.channels.whatsapp_window import provider_timestamp, utcnow

router = APIRouter(tags=["whatsapp"])

WHATSAPP_TEXT_LIMIT = 4096
DEFAULT_WHATSAPP_API_VERSION = "v23.0"


class WhatsAppChannelPayload(TypedDict, total=False):
    access_token: str
    phone_number_id: str
    verify_token: str
    app_secret: str
    api_version: str


def _split_text(text: str, limit: int = WHATSAPP_TEXT_LIMIT) -> list[str]:
    remaining = text or ""
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        segment = remaining[:limit]
        cut = max(segment.rfind("\n\n"), segment.rfind("\n"), segment.rfind(" "))
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return chunks or [""]


def _verify_signature(app_secret: str, body: bytes, signature: str | None) -> bool:
    if not app_secret or signature in (None, "") or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.encode())


def _extract_message_text(message: JsonObject) -> str:
    msg_type = json_as_str_or(message.get("type"))
    if msg_type == "text":
        return json_as_str_or(json_object_from(message.get("text")).get("body")).strip()
    if msg_type == "button":
        return json_as_str_or(json_object_from(message.get("button")).get("text")).strip()
    if msg_type == "interactive":
        interactive = json_object_from(message.get("interactive"))
        if not interactive:
            return ""
        button_reply = json_object_from(interactive.get("button_reply"))
        list_reply = json_object_from(interactive.get("list_reply"))
        return (json_as_str_or(button_reply.get("title")) or json_as_str_or(list_reply.get("title"))).strip()
    return ""


async def _send_whatsapp_messages(
    config: ChannelConfigRecord, to_phone: str, text: str, *, strict: bool = False
) -> None:
    token = (config.app_secret or "").strip()
    phone_number_id = (config.app_id or "").strip()
    if not token or not phone_number_id:
        raise RuntimeError("WhatsApp channel is not fully configured")

    api_version = str((config.extra_config or {}).get("api_version") or DEFAULT_WHATSAPP_API_VERSION).strip()
    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"

    async with httpx.AsyncClient(timeout=20) as client:
        for chunk in _split_text(text):
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": to_phone,
                    "type": "text",
                    "text": {"preview_url": False, "body": chunk},
                },
            )
            if strict:
                _ = resp.raise_for_status()
                data = json_object_from_response(resp)
                messages = data.get("messages")
                if bool(data.get("error")) or not isinstance(messages, list) or not messages:
                    raise RuntimeError("WhatsApp did not acknowledge the message")
                if any(not bool(json_as_str(json_object_from(message).get("id"))) for message in messages):
                    raise RuntimeError("WhatsApp returned no message id")
            if resp.status_code >= 400:
                raise RuntimeError(f"WhatsApp send failed: {resp.text[:300]}")


@router.post("/agents/{agent_id}/whatsapp-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_whatsapp_channel(
    agent_id: uuid.UUID, data: WhatsAppChannelPayload, current_user: UserRecord = Depends(get_current_user)
) -> ChannelConfigOut:
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    access_token = str(data.get("access_token") or "").strip()
    phone_number_id = str(data.get("phone_number_id") or "").strip()
    verify_token = str(data.get("verify_token") or "").strip()
    app_secret = str(data.get("app_secret") or "").strip()
    api_version = str(data.get("api_version") or DEFAULT_WHATSAPP_API_VERSION).strip()

    if not access_token or not phone_number_id or not verify_token or not app_secret:
        raise HTTPException(
            status_code=422, detail="access_token, phone_number_id, verify_token, and app_secret are required"
        )

    extra_config: JsonObject = {"api_version": api_version}
    existing = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.WHATSAPP)
    if existing:
        config = await channel_config_dao.update(
            db_obj=existing,
            obj_in={
                "app_id": phone_number_id,
                "app_secret": access_token,
                "verification_token": verify_token,
                "encrypt_key": app_secret or None,
                "extra_config": extra_config,
                "is_configured": True,
            },
        )
        return ChannelConfigOut.model_validate(config or existing)

    config = await channel_config_dao.create(
        obj_in={
            "agent_id": agent_id,
            "channel_type": "whatsapp",
            "app_id": phone_number_id,
            "app_secret": access_token,
            "verification_token": verify_token,
            "encrypt_key": app_secret or None,
            "extra_config": extra_config,
            "is_configured": True,
        }
    )
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/whatsapp-channel", response_model=ChannelConfigOut)
async def get_whatsapp_channel(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> ChannelConfigOut:
    _ = await check_agent_access(current_user, agent_id)
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.WHATSAPP)
    if not config:
        raise HTTPException(status_code=404, detail="WhatsApp not configured")
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/whatsapp-channel/webhook-url", response_model=None)
async def get_whatsapp_webhook_url(agent_id: uuid.UUID, request: Request) -> WebhookUrlResponse:
    from app.services.platform_service import platform_service

    public_base = await platform_service.get_public_base_url(request)
    return {"webhook_url": f"{public_base}/api/channel/whatsapp/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/whatsapp-channel", status_code=204, response_model=None)
async def delete_whatsapp_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)) -> None:
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")

    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.WHATSAPP)
    if not config:
        raise HTTPException(status_code=404, detail="WhatsApp not configured")
    _ = await channel_config_dao.delete(record_id=config.id)


@router.get("/channel/whatsapp/{agent_id}/webhook")
async def whatsapp_verify_webhook(
    agent_id: uuid.UUID,
    hub_mode: str = Query("", alias="hub.mode"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
) -> Response:
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.WHATSAPP)
    if not config:
        return Response(status_code=404)

    if (
        hub_mode == "subscribe"
        and hub_verify_token
        and hmac.compare_digest(hub_verify_token, config.verification_token or "")
    ):
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(status_code=403)


@router.post("/channel/whatsapp/{agent_id}/webhook", response_model=None)
async def whatsapp_event_webhook(agent_id: uuid.UUID, request: Request) -> Response | OkAcknowledgement:
    body = await read_bounded_body(request, CHANNEL_WEBHOOK_MAX_BODY_SIZE)
    if body is None:
        return Response(status_code=413)
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.WHATSAPP)
    if not config:
        return Response(status_code=404)

    app_secret = (config.encrypt_key or "").strip()
    signature = request.headers.get("x-hub-signature-256")
    if not _verify_signature(app_secret, body, signature):
        return Response(status_code=401)
    received_at = utcnow()

    try:
        payload = json_loads_object(body)
    except ValueError, UnicodeError:
        return Response(status_code=401)
    entries = payload.get("entry")
    failed = False
    for entry_raw in entries if isinstance(entries, list) else []:
        entry = json_object_from(entry_raw)
        changes = entry.get("changes")
        for change_raw in changes if isinstance(changes, list) else []:
            change = json_object_from(change_raw)
            value = json_object_from(change.get("value"))
            messages = value.get("messages")
            message_items = messages if isinstance(messages, list) else []
            metadata = json_object_from(value.get("metadata"))
            account = json_as_str_or(metadata.get("phone_number_id")).strip()
            if not account or account != (config.app_id or "").strip():
                continue

            for message_raw in message_items:
                message = json_object_from(message_raw)
                message_id = json_as_str_or(message.get("id")).strip()
                user_text = _extract_message_text(message)
                sender_phone = json_as_str_or(message.get("from")).strip()
                if not user_text or not sender_phone:
                    continue

                if not message_id:
                    failed = True
                    continue
                from fastapi import HTTPException

                from app.services.channel_user_service import ChannelUserResolutionError
                from app.services.channels.submissions import (
                    ChannelEvent,
                    ChannelEventConflictError,
                    accept_channel_event,
                )
                from app.services.quota_guard import AgentExpired, QuotaExceeded

                native = ChannelEvent(
                    config_id=config.id,
                    agent_id=agent_id,
                    provider="whatsapp",
                    event_key=message_id,
                    sender_id=sender_phone,
                    conversation_id=f"whatsapp_{sender_phone}",
                    text=user_text,
                    kind=json_as_str_or(message.get("type"), "text"),
                    provider_timestamp=provider_timestamp(message.get("timestamp"), received_at),
                    provider_account_id=account,
                    provider_timestamp_source=json.dumps(message.get("timestamp"), separators=(",", ":")),
                )
                try:
                    await accept_channel_event(native)
                except ChannelUserResolutionError, AgentExpired, QuotaExceeded:
                    continue
                except HTTPException as exc:
                    failed = failed or exc.status_code >= 500
                except ChannelEventConflictError:
                    failed = True
                except Exception:
                    failed = True
                    logger.exception("[WhatsApp] Inbound acceptance failed for agent %s", agent_id)

    return Response(status_code=503) if failed else {"ok": True}
