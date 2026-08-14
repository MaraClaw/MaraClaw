"""WhatsApp Cloud API channel routes."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime
from typing import Any, TypedDict

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.core.json_types import JsonObject
from app.core.logging import logger
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.dao.agent_dao import agent_dao
from app.dao.channel_config_dao import channel_config_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.records.channel_config import ChannelConfigRecord
from app.records.user import UserRecord
from app.schemas.schemas import ChannelConfigOut
from app.services.channel_session import find_or_create_channel_session
from app.services.channel_user_service import channel_user_service

router = APIRouter(tags=["whatsapp"])

WHATSAPP_TEXT_LIMIT = 4096
DEFAULT_WHATSAPP_API_VERSION = "v23.0"
DEFAULT_CONTEXT_WINDOW_SIZE = 100
class WhatsAppText(TypedDict, total=False):
    body: str


class WhatsAppButton(TypedDict, total=False):
    text: str


class WhatsAppReply(TypedDict, total=False):
    title: str


class WhatsAppInteractive(TypedDict, total=False):
    button_reply: WhatsAppReply
    list_reply: WhatsAppReply


class WhatsAppMessage(TypedDict, total=False):
    type: str
    text: WhatsAppText
    button: WhatsAppButton
    interactive: WhatsAppInteractive


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
    if not app_secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_message_text(message: WhatsAppMessage) -> str:
    msg_type = message.get("type")
    if msg_type == "text":
        text = message.get("text")
        return ((text.get("body") if text else "") or "").strip()
    if msg_type == "button":
        button = message.get("button")
        return ((button.get("text") if button else "") or "").strip()
    if msg_type == "interactive":
        interactive = message.get("interactive")
        if not interactive:
            return ""
        button_reply = interactive.get("button_reply")
        list_reply = interactive.get("list_reply")
        return (
            (button_reply.get("title") if button_reply else "") or (list_reply.get("title") if list_reply else "") or ""
        ).strip()
    return ""


async def _send_whatsapp_messages(config: ChannelConfigRecord, to_phone: str, text: str) -> None:
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
            if resp.status_code >= 400:
                raise RuntimeError(f"WhatsApp send failed: {resp.text[:300]}")


@router.post("/agents/{agent_id}/whatsapp-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_whatsapp_channel(
    agent_id: uuid.UUID, data: WhatsAppChannelPayload, current_user: UserRecord = Depends(get_current_user)
):
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    access_token = str(data.get("access_token") or "").strip()
    phone_number_id = str(data.get("phone_number_id") or "").strip()
    verify_token = str(data.get("verify_token") or "").strip()
    app_secret = str(data.get("app_secret") or "").strip()
    api_version = str(data.get("api_version") or DEFAULT_WHATSAPP_API_VERSION).strip()

    if not access_token or not phone_number_id or not verify_token:
        raise HTTPException(status_code=422, detail="access_token, phone_number_id, and verify_token are required")

    extra_config: JsonObject = {"api_version": api_version}
    existing = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="whatsapp")
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
async def get_whatsapp_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    _ = await check_agent_access(current_user, agent_id)
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="whatsapp")
    if not config:
        raise HTTPException(status_code=404, detail="WhatsApp not configured")
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/whatsapp-channel/webhook-url")
async def get_whatsapp_webhook_url(agent_id: uuid.UUID, request: Request, db: object | None = None):
    from app.services.platform_service import platform_service

    public_base = await platform_service.get_public_base_url(db, request)
    return {"webhook_url": f"{public_base}/api/channel/whatsapp/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/whatsapp-channel", status_code=204)
async def delete_whatsapp_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")

    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="whatsapp")
    if not config:
        raise HTTPException(status_code=404, detail="WhatsApp not configured")
    _ = await channel_config_dao.delete(id=config.id)


@router.get("/channel/whatsapp/{agent_id}/webhook")
async def whatsapp_verify_webhook(
    agent_id: uuid.UUID,
    hub_mode: str = Query("", alias="hub.mode"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
):
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="whatsapp")
    if not config:
        return Response(status_code=404)

    if (
        hub_mode == "subscribe"
        and hub_verify_token
        and hmac.compare_digest(hub_verify_token, config.verification_token or "")
    ):
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(status_code=403)


@router.post("/channel/whatsapp/{agent_id}/webhook")
async def whatsapp_event_webhook(agent_id: uuid.UUID, request: Request):
    body = await request.body()
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="whatsapp")
    if not config:
        return Response(status_code=404)

    app_secret = (config.encrypt_key or "").strip()
    signature = request.headers.get("x-hub-signature-256")
    if app_secret and not _verify_signature(app_secret, body, signature):
        return Response(status_code=401)

    payload: dict[str, Any] = await request.json()
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            messages = value.get("messages") or []
            contacts = value.get("contacts") or []
            contact_name = ""
            if contacts:
                contact_name = str((contacts[0].get("profile") or {}).get("name") or "").strip()

            for message in messages:
                message_id = str(message.get("id") or "").strip()
                from app.services.channels import dedup as channel_dedup

                if message_id and await channel_dedup.already_processed_shared("whatsapp", message_id):
                    continue
                if message_id:
                    await channel_dedup.mark_processed_shared("whatsapp", message_id)

                user_text = _extract_message_text(message)
                sender_phone = str(message.get("from") or "").strip()
                if not user_text or not sender_phone:
                    continue

                from app.api.feishu import _call_llm_with_config, _load_agent_and_model
                from app.services.llm.utils import convert_chat_messages_to_llm_format as _conv

                agent_obj = await agent_dao.get(agent_id)
                if not agent_obj:
                    continue

                platform_user = await channel_user_service.resolve_channel_user(
                    db=None,
                    agent=agent_obj,
                    channel_type="whatsapp",
                    external_user_id=sender_phone,
                    extra_info={"name": contact_name or f"WhatsApp User {sender_phone[-6:]}"},
                )
                platform_user_id = platform_user.id
                conv_id = f"whatsapp_{sender_phone}"
                sess = await find_or_create_channel_session(
                    db=None,
                    agent_id=agent_id,
                    user_id=platform_user_id,
                    external_conv_id=conv_id,
                    source_channel="whatsapp",
                    first_message_title=user_text,
                )
                session_conv_id = str(sess.id)
                ctx_size = agent_obj.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE
                history_msgs = await chat_message_dao.list_recent(
                    agent_id=agent_id,
                    conversation_id=session_conv_id,
                    limit=ctx_size,
                )
                history = _conv(history_msgs)

                _ = await chat_message_dao.insert_message(
                    agent_id=agent_id,
                    user_id=platform_user_id,
                    role="user",
                    content=user_text,
                    conversation_id=session_conv_id,
                )
                _ = await chat_session_dao.update(db_obj=sess, obj_in={"last_message_at": datetime.now(UTC)})

                _agent_model, _llm_model, _fallback_model = await _load_agent_and_model(None, agent_id)

                try:
                    reply_text = await _call_llm_with_config(
                        _agent_model,
                        _llm_model,
                        _fallback_model,
                        agent_id,
                        user_text,
                        history=history,
                        user_id=platform_user_id,
                        session_id=session_conv_id,
                    )
                except Exception as exc:
                    logger.exception(f"[WhatsApp] LLM failed for agent {agent_id}: {exc}")
                    reply_text = "Sorry, I encountered an error processing your message."

                try:
                    await _send_whatsapp_messages(config, sender_phone, reply_text)
                    _ = await chat_message_dao.insert_message(
                        agent_id=agent_id,
                        user_id=platform_user_id,
                        role="assistant",
                        content=reply_text,
                        conversation_id=session_conv_id,
                    )
                    try:
                        fresh = await chat_session_dao.get(uuid.UUID(session_conv_id))
                        if fresh:
                            _ = await chat_session_dao.update(db_obj=fresh, obj_in={"last_message_at": datetime.now(UTC)})
                    except ValueError, TypeError:
                        pass
                except Exception as exc:
                    logger.exception(f"[WhatsApp] Send failed for agent {agent_id}: {exc}")

    return {"ok": True}
