"""WeCom (Enterprise WeChat) Channel API routes.

Provides Config CRUD and webhook-based message handling with AES encryption.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import struct
import uuid
from dataclasses import dataclass
from typing import TypedDict, overload
from xml.parsers.expat import ExpatError, ParserCreate

import httpx
from Crypto.Cipher import AES
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from app.api.background_tasks import schedule_background_task
from app.core.json_types import (
    JsonObject,
    json_as_int,
    json_as_str,
    json_as_str_or,
    json_object_from,
    json_object_from_response,
)
from app.core.logging import logger
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.request_body import CHANNEL_WEBHOOK_MAX_BODY_SIZE, read_bounded_body
from app.core.security import get_current_user
from app.dao.channel_config_dao import channel_config_dao
from app.dao.identity_provider_dao import identity_provider_dao
from app.records.channel_config import ChannelConfigRecord
from app.records.user import UserRecord
from app.schemas.channel_response_contracts import WebhookUrlResponse
from app.schemas.schemas import ChannelConfigOut
from app.services.auth_registry import auth_provider_registry
from app.services.platform_service import platform_service
from app.services.sso_scan_sessions import authorize_scan_session, load_authorizable_session, parse_scan_state
from app.services.wecom_stream import wecom_stream_manager

router = APIRouter(tags=["wecom"])


class WeComChannelPayload(TypedDict, total=False):
    bot_id: str
    bot_secret: str
    corp_id: str
    wecom_agent_id: str
    secret: str
    token: str
    encoding_aes_key: str


@dataclass(frozen=True, slots=True)
class WeComXmlFields:
    text_by_tag: dict[str, str]

    @overload
    def findtext(self, tag: str, default: str) -> str: ...

    @overload
    def findtext(self, tag: str, default: None = None) -> str | None: ...

    def findtext(self, tag: str, default: str | None = None) -> str | None:
        return self.text_by_tag.get(tag) or default


# ─── WeCom AES Crypto ──────────────────────────────────


def _pad(text: bytes) -> bytes:
    """PKCS7 padding for AES-CBC."""
    block_size = 32
    pad_len = block_size - (len(text) % block_size)
    return text + bytes([pad_len] * pad_len)


def _unpad(text: bytes) -> bytes:
    """Remove PKCS7 padding."""
    pad_len = text[-1]
    return text[:-pad_len]


def _decrypt_msg(encrypt_key: str, encrypted_text: str) -> tuple[str, str]:
    """Decrypt a WeCom encrypted message.

    Returns (decrypted_xml, corp_id)
    """
    aes_key = base64.b64decode(encrypt_key + "=")
    iv = aes_key[:16]
    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    decrypted = _unpad(cipher.decrypt(base64.b64decode(encrypted_text)))
    # Skip 16 random bytes, then 4 bytes msg_length (network order)
    msg_len = struct.unpack("!I", decrypted[16:20])[0]
    msg_content = decrypted[20 : 20 + msg_len].decode("utf-8")
    corp_id = decrypted[20 + msg_len :].decode("utf-8")
    return msg_content, corp_id


def _encrypt_msg(encrypt_key: str, reply_msg: str, corp_id: str) -> str:
    """Encrypt a reply message for WeCom."""
    aes_key = base64.b64decode(encrypt_key + "=")
    iv = aes_key[:16]
    msg_bytes = reply_msg.encode("utf-8")
    buf = os.urandom(16) + struct.pack("!I", len(msg_bytes)) + msg_bytes + corp_id.encode("utf-8")
    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(_pad(buf))
    return base64.b64encode(encrypted).decode("utf-8")


def _verify_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """Generate WeCom message signature."""
    items = sorted([token, timestamp, nonce, encrypt])
    return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()  # noqa: S324 - required by WeCom callback signature protocol


def _parse_wecom_xml(xml: bytes | str) -> WeComXmlFields:
    raw = xml.encode("utf-8") if isinstance(xml, str) else xml
    if b"<!DOCTYPE" in raw.upper():
        raise ValueError("DOCTYPE is not allowed in WeCom XML")

    field_parts: dict[str, list[str]] = {}
    depth = 0
    active_tag: str | None = None

    def start_element(name: str, _attributes: dict[str, str]) -> None:
        nonlocal active_tag, depth
        depth += 1
        if depth == 2 and name not in field_parts:
            field_parts[name] = []
            active_tag = name
        elif depth == 3:
            active_tag = None

    def character_data(text: str) -> None:
        if depth == 2 and active_tag is not None:
            field_parts[active_tag].append(text)

    def end_element(_name: str) -> None:
        nonlocal active_tag, depth
        if depth == 2:
            active_tag = None
        depth -= 1

    try:
        parser = ParserCreate()
        parser.StartElementHandler = start_element
        parser.CharacterDataHandler = character_data
        parser.EndElementHandler = end_element
        _ = parser.Parse(raw, True)
    except ExpatError as exc:
        raise ValueError("Invalid WeCom XML") from exc
    return WeComXmlFields({tag: "".join(parts) for tag, parts in field_parts.items()})


# ─── WeCom Domain Verification File Hosting ────────────

# WeCom requires that each self-built app's trusted domain host a
# verification file at: https://domain/WW_verify_<token>.txt
# The file content is just the token string (plain text).
#
# For multi-tenant SaaS, we don't want every tenant to have their own server.
# Instead, tenants paste their verification token into the enterprise settings,
# and this endpoint serves the correct file content for any known token.
#
# Nginx config required to route requests at the root path:
#   location ~ ^/(WW_verify_[A-Za-z0-9_.-]{1,64}\.txt)$ {
#       proxy_pass http://backend:8000/api/wecom-verify/$1;
#   }

_VERIFY_FILENAME_RE = re.compile(r"^WW_verify_[A-Za-z0-9_]{1,64}\.txt$")


@router.get("/wecom-verify/{filename}")
async def serve_wecom_verify_file(filename: str) -> Response:
    """Serve a WeCom domain verification file.

    Looks across all active WeCom IdentityProviders for one whose config
    contains the requested filename. Returns the verification content as
    plain text so WeCom's ownership-check bot can confirm it.

    Security: filename is validated against a strict whitelist regex before
    any DB lookup to prevent path traversal or injection attacks.
    """
    # Strict allowlist: only WW_verify_*.txt filenames are legal
    if not _VERIFY_FILENAME_RE.fullmatch(filename):
        return Response(status_code=404)

    providers = await identity_provider_dao.list_active_by_type("wecom")

    for provider in providers:
        config = json_object_from(provider.config)
        verify_files = config.get("wecom_verify_files")
        content = json_object_from(verify_files).get(filename) if isinstance(verify_files, dict) else None
        if isinstance(content, str):
            logger.info(f"[WeCom Verify] Serving {filename} for tenant {provider.tenant_id}")
            return Response(content=content, media_type="text/plain")

    return Response(status_code=404)


# ─── Config CRUD ────────────────────────────────────────


@router.post("/agents/{agent_id}/wecom-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_wecom_channel(
    agent_id: uuid.UUID, data: WeComChannelPayload, current_user: UserRecord = Depends(get_current_user)
) -> ChannelConfigOut:
    """Configure WeCom bot for an agent.

    Supports two modes:
    - WebSocket (AI Bot): bot_id + bot_secret (no callback URL needed)
    - Webhook (legacy): corp_id, secret, token, encoding_aes_key
    """
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    # WebSocket mode fields (AI Bot)
    bot_id = data.get("bot_id", "").strip()
    bot_secret = data.get("bot_secret", "").strip()

    # Legacy webhook mode fields
    corp_id = data.get("corp_id", "").strip()
    wecom_agent_id = data.get("wecom_agent_id", "").strip()
    secret = data.get("secret", "").strip()
    token = data.get("token", "").strip()
    encoding_aes_key = data.get("encoding_aes_key", "").strip()

    # At least one mode must be configured
    has_ws_mode = bool(bot_id and bot_secret)
    has_webhook_mode = bool(corp_id and secret and token and encoding_aes_key)
    if not has_ws_mode and not has_webhook_mode:
        raise HTTPException(
            status_code=422,
            detail="Either bot_id+bot_secret (WebSocket) or corp_id+secret+token+encoding_aes_key (Webhook) required",
        )

    extra_config: JsonObject = {
        "wecom_agent_id": wecom_agent_id,
        "bot_id": bot_id,
        "bot_secret": bot_secret,
        "connection_mode": "websocket" if has_ws_mode else "webhook",
    }

    existing = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="wecom")
    if existing:
        config = await channel_config_dao.update(
            db_obj=existing,
            obj_in={
                "app_id": corp_id,
                "app_secret": secret,
                "encrypt_key": encoding_aes_key,
                "verification_token": token,
                "extra_config": extra_config,
                "is_configured": True,
                "is_connected": False,
            },
        )
        config_out = ChannelConfigOut.model_validate(config or existing)
    else:
        config = await channel_config_dao.create(
            obj_in={
                "agent_id": agent_id,
                "channel_type": "wecom",
                "app_id": corp_id,
                "app_secret": secret,
                "encrypt_key": encoding_aes_key,
                "verification_token": token,
                "extra_config": extra_config,
                "is_configured": True,
                "is_connected": False,
            }
        )
        config_out = ChannelConfigOut.model_validate(config)

    try:
        if has_ws_mode:
            schedule_background_task(
                wecom_stream_manager.start_client(agent_id, bot_id, bot_secret), "WeCom start_client"
            )
            logger.info(f"[WeCom] WebSocket client start triggered for agent {agent_id}")
        else:
            schedule_background_task(wecom_stream_manager.stop_client(agent_id), "WeCom stop_client")
            logger.info(f"[WeCom] WebSocket client stop triggered for agent {agent_id}")
    except Exception as e:
        logger.error(f"[WeCom] Failed to update WebSocket client state: {e}")

    return config_out


@router.get("/agents/{agent_id}/wecom-channel", response_model=ChannelConfigOut)
async def get_wecom_channel(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> ChannelConfigOut:
    _ = await check_agent_access(current_user, agent_id)
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="wecom")
    if not config:
        raise HTTPException(status_code=404, detail="WeCom not configured")

    config_out = ChannelConfigOut.model_validate(config)
    extra = json_object_from(config.extra_config)
    if extra.get("connection_mode") == "websocket":
        config_out.is_connected = wecom_stream_manager.status().get(str(agent_id), False)
    else:
        config_out.is_connected = False
    return config_out


@router.get("/agents/{agent_id}/wecom-channel/webhook-url", response_model=None)
async def get_wecom_webhook_url(agent_id: uuid.UUID, request: Request) -> WebhookUrlResponse:
    public_base = await platform_service.get_public_base_url(request)
    return {"webhook_url": f"{public_base}/api/channel/wecom/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/wecom-channel", status_code=204, response_model=None)
async def delete_wecom_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)) -> None:
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="wecom")
    if not config:
        raise HTTPException(status_code=404, detail="WeCom not configured")
    await wecom_stream_manager.stop_client(agent_id)
    _ = await channel_config_dao.delete(id=config.id)


# ─── Event Webhook ──────────────────────────────────────


@router.get("/channel/wecom/{agent_id}/webhook")
async def wecom_verify_webhook(
    agent_id: uuid.UUID, msg_signature: str = "", timestamp: str = "", nonce: str = "", echostr: str = ""
) -> Response:
    """Handle WeCom callback URL verification (GET request)."""
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="wecom")
    if not config:
        return Response(status_code=404)

    token = config.verification_token or ""
    encoding_aes_key = config.encrypt_key or ""

    # Verify signature
    expected_sig = _verify_signature(token, timestamp, nonce, echostr)
    if expected_sig != msg_signature:
        logger.warning(f"[WeCom] Signature mismatch: expected={expected_sig}, got={msg_signature}")
        return Response(status_code=403)

    # Decrypt echostr and return plaintext
    try:
        decrypted, _ = _decrypt_msg(encoding_aes_key, echostr)
        return Response(content=decrypted, media_type="text/plain")
    except Exception as e:
        logger.error(f"[WeCom] Failed to decrypt echostr: {e}")
        return Response(status_code=500)


@router.post("/channel/wecom/{agent_id}/webhook")
async def wecom_event_webhook(
    agent_id: uuid.UUID, request: Request, msg_signature: str = "", timestamp: str = "", nonce: str = ""
) -> Response:
    """Handle WeCom message callback (POST request with encrypted XML)."""
    body_bytes = await read_bounded_body(request, CHANNEL_WEBHOOK_MAX_BODY_SIZE)
    if body_bytes is None:
        return Response(status_code=413)

    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="wecom")
    if not config:
        return Response(status_code=404)

    token = config.verification_token or ""
    encoding_aes_key = config.encrypt_key or ""
    # Parse encrypted XML body
    try:
        root = _parse_wecom_xml(body_bytes)
        encrypt_text = root.findtext("Encrypt", "")
    except Exception as e:
        logger.error(f"[WeCom] Failed to parse XML body: {e}")
        return Response(content="success", media_type="text/plain")

    # Verify signature
    expected_sig = _verify_signature(token, timestamp, nonce, encrypt_text)
    if expected_sig != msg_signature:
        logger.warning("[WeCom] Signature mismatch on POST")
        return Response(status_code=403)

    # Decrypt message
    try:
        decrypted_xml, _recv_corp_id = _decrypt_msg(encoding_aes_key, encrypt_text)
    except Exception as e:
        logger.error(f"[WeCom] Failed to decrypt message: {e}")
        return Response(content="success", media_type="text/plain")

    logger.info(f"[WeCom] Decrypted event for {agent_id}")

    # Parse decrypted message XML
    try:
        msg_root = _parse_wecom_xml(decrypted_xml)
    except Exception as e:
        logger.error(f"[WeCom] Failed to parse decrypted XML: {e}")
        return Response(content="success", media_type="text/plain")

    msg_type = msg_root.findtext("MsgType", "")
    from_user = msg_root.findtext("FromUserName", "")  # WeCom userid
    msg_id = msg_root.findtext("MsgId", "")
    open_kfid = msg_root.findtext("OpenKfId", "")
    token = msg_root.findtext("Token", "")
    # Group chat ID - present when message comes from a WeCom group
    chat_id = msg_root.findtext("ChatId", "")

    logger.info(f"[WeCom] Message type={msg_type}, from={from_user}, msg_id={msg_id}, chat_id={chat_id or 'N/A'}")

    if msg_type == "text":
        user_text = msg_root.findtext("Content", "").strip()
        if not user_text:
            return Response(content="success", media_type="text/plain")

        try:
            await _process_wecom_text(agent_id, config, from_user, user_text, chat_id=chat_id, message_id=msg_id)
        except Exception:
            logger.exception("[WeCom] Text acceptance failed for agent {}", agent_id)
            return Response(status_code=503)

    elif msg_type == "event":
        event = msg_root.findtext("Event", "")
        if event == "kf_msg_or_event":
            try:
                await _process_wecom_kf_event(agent_id, config, token, open_kfid)
            except Exception:
                logger.exception("[WeCom] KF acceptance failed for agent {}", agent_id)
                return Response(status_code=503)
        else:
            logger.info(f"[WeCom] Received event: {event} (not handled)")

    elif msg_type in ("image", "file"):
        # TODO: Handle image/file messages in future
        logger.info(f"[WeCom] Received {msg_type} message (not yet handled)")

    return Response(content="success", media_type="text/plain")


async def _process_wecom_kf_event(
    agent_id: uuid.UUID,
    config_obj: ChannelConfigRecord,
    token: str,
    open_kfid: str | None = None,
):
    _ = config_obj
    try:
        config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="wecom")
        if not config:
            return

        async with httpx.AsyncClient(timeout=10) as client:
            tok_resp = await client.get(
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                params={"corpid": config.app_id, "corpsecret": config.app_secret},
            )
            token_data = json_object_from_response(tok_resp)
            access_token = json_as_str(token_data.get("access_token"))
            if access_token in (None, ""):
                raise HTTPException(status_code=503, detail="WeCom KF token unavailable")

            current_cursor = token
            has_more = 1

            while has_more:
                payload: dict[str, int | str] = {"limit": 20}
                if open_kfid not in (None, ""):
                    payload["open_kfid"] = open_kfid

                if current_cursor.startswith("ENC"):
                    payload["token"] = current_cursor
                else:
                    payload["cursor"] = current_cursor

                logger.info(f"[WeCom KF] Calling sync_msg with payload: {payload}")
                sync_resp = await client.post(
                    f"https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg?access_token={access_token}", json=payload
                )
                sync_data = json_object_from_response(sync_resp)
                if sync_data.get("errcode") != 0:
                    raise HTTPException(status_code=503, detail="WeCom KF sync failed")

                has_more = json_as_int(sync_data.get("has_more"))
                current_cursor = json_as_str_or(sync_data.get("next_cursor"))

                msg_list = sync_data.get("msg_list")
                for raw_msg in msg_list if isinstance(msg_list, list) else []:
                    msg = json_object_from(raw_msg)
                    if json_as_int(msg.get("origin")) == 3 and json_as_str(msg.get("msgtype")) == "text":
                        mid = msg.get("msgid")
                        text = json_as_str_or(json_object_from(msg.get("text")).get("content")).strip()
                        if text:
                            external_userid = json_as_str_or(msg.get("external_userid"))
                            logger.info(f"[WeCom KF] Found msg from {external_userid}: {text[:20]}...")
                            await _process_wecom_text(
                                agent_id,
                                config,
                                external_userid,
                                text,
                                is_kf=True,
                                open_kfid=json_as_str(msg.get("open_kfid")),
                                kf_msg_id=json_as_str(mid),
                            )
                if not has_more:
                    break
    except Exception as e:
        logger.error(f"[WeCom KF] Acceptance failed: {e}")
        raise


async def _process_wecom_text(
    agent_id: uuid.UUID,
    config: ChannelConfigRecord,
    from_user: str,
    user_text: str,
    is_kf: bool = False,
    open_kfid: str | None = None,
    kf_msg_id: str | None = None,
    chat_id: str = "",
    message_id: str = "",
):
    native_id = kf_msg_id or message_id
    if not native_id:
        raise HTTPException(status_code=400, detail="WeCom message ID is required")
    from app.services.channels.submissions import ChannelEvent, accept_channel_event

    await accept_channel_event(
        ChannelEvent(
            config_id=config.id,
            agent_id=agent_id,
            provider="wecom",
            event_key=json.dumps([config.app_id, open_kfid or chat_id, native_id], separators=(",", ":")),
            sender_id=from_user,
            conversation_id=f"wecom_group_{chat_id}" if chat_id else f"wecom_p2p_{from_user}",
            text=user_text,
            kind="kf_text" if is_kf else "text",
            is_group=bool(chat_id),
        )
    )


# ─── OAuth Callback (SSO) ──────────────────────────────


@router.get("/auth/wecom/callback")
async def wecom_callback(code: str, state: str | None = None) -> HTMLResponse:
    sid = parse_scan_state(state)
    if sid is None:
        return HTMLResponse("Auth failed: invalid SSO session")

    try:
        session = await load_authorizable_session(sid)
        if session is None:
            return HTMLResponse("Auth failed: invalid SSO session")
        tenant_id = session.tenant_id
        provider = await identity_provider_dao.get_by_type_and_tenant("wecom", tenant_id)
        if not provider:
            return HTMLResponse("Auth failed: WeCom provider not configured for this tenant")
        provider_tenant_id = str(tenant_id) if tenant_id else (str(provider.tenant_id) if provider.tenant_id else None)
        auth_provider = await auth_provider_registry.get_provider("wecom", provider_tenant_id)
        if not auth_provider:
            return HTMLResponse("Auth failed: WeCom provider unavailable")

        token_data = await auth_provider.exchange_code_for_token(code)
        access_token_str = token_data.get("access_token")
        if not isinstance(access_token_str, str) or not access_token_str:
            return HTMLResponse("Auth failed: Token error")

        user_info = await auth_provider.get_user_info(access_token_str)
        if user_info.provider_user_id in (None, ""):
            return HTMLResponse("Auth failed: No UserId returned")

        user, _is_new = await auth_provider.find_or_create_user(user_info, tenant_id=provider_tenant_id)
        if not await authorize_scan_session(sid, user=user, provider_type="wecom"):
            return HTMLResponse("Auth failed: invalid SSO session")
    except Exception:
        logger.error("WeCom login/register error")
        return HTMLResponse("Auth failed: Authentication error")

    return HTMLResponse(
        f"""<html><head><meta charset="utf-8" /></head>
        <body style="font-family: sans-serif; padding: 24px;">
            <div>SSO login successful. Redirecting...</div>
            <script>window.location.href = "/sso/entry?sid={sid}&complete=1";</script>
        </body></html>"""
    )
