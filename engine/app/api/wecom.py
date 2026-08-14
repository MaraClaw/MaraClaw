"""WeCom (Enterprise WeChat) Channel API routes.

Provides Config CRUD and webhook-based message handling with AES encryption.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import struct
import time
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypedDict, overload
from xml.parsers.expat import ExpatError, ParserCreate

import httpx
from Crypto.Cipher import AES
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from app.core.json_types import JsonObject
from app.core.logging import logger
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import create_access_token, get_current_user
from app.dao.agent_dao import agent_dao
from app.dao.channel_config_dao import channel_config_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.identity_provider_dao import identity_provider_dao
from app.dao.sso_scan_session_dao import sso_scan_session_dao
from app.records.channel_config import ChannelConfigRecord
from app.records.user import UserRecord
from app.schemas.schemas import ChannelConfigOut
from app.services.activity_logger import log_activity
from app.services.auth_registry import auth_provider_registry
from app.services.channel_session import find_or_create_channel_session
from app.services.channel_user_service import channel_user_service
from app.services.platform_service import platform_service
from app.services.wecom_stream import wecom_stream_manager

router = APIRouter(tags=["wecom"])
_background_tasks: set[asyncio.Task[None]] = set()
DEFAULT_CONTEXT_WINDOW_SIZE = 100


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


def _schedule_background(coro: Awaitable[object]) -> None:
    async def run() -> None:
        await coro

    task = asyncio.create_task(run())
    _background_tasks.add(task)

    def observe_completion(completed_task: asyncio.Task[None]) -> None:
        _background_tasks.discard(completed_task)
        if completed_task.cancelled():
            logger.debug("WeCom background task cancelled")
            return
        if error := completed_task.exception():
            logger.opt(exception=error).error("WeCom background task failed")

    task.add_done_callback(observe_completion)


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
        parser.Parse(raw, True)
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
async def serve_wecom_verify_file(filename: str):
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
        config = provider.config or {}
        verify_files = config.get("wecom_verify_files")
        content = verify_files.get(filename) if isinstance(verify_files, dict) else None
        if isinstance(content, str):
            logger.info(f"[WeCom Verify] Serving {filename} for tenant {provider.tenant_id}")
            return Response(content=content, media_type="text/plain")

    return Response(status_code=404)


# ─── Config CRUD ────────────────────────────────────────


@router.post("/agents/{agent_id}/wecom-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_wecom_channel(
    agent_id: uuid.UUID, data: WeComChannelPayload, current_user: UserRecord = Depends(get_current_user)
):
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
            _schedule_background(wecom_stream_manager.start_client(agent_id, bot_id, bot_secret))
            logger.info(f"[WeCom] WebSocket client start triggered for agent {agent_id}")
        else:
            _schedule_background(wecom_stream_manager.stop_client(agent_id))
            logger.info(f"[WeCom] WebSocket client stop triggered for agent {agent_id}")
    except Exception as e:
        logger.error(f"[WeCom] Failed to update WebSocket client state: {e}")

    return config_out


@router.get("/agents/{agent_id}/wecom-channel", response_model=ChannelConfigOut)
async def get_wecom_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    await check_agent_access(current_user, agent_id)
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="wecom")
    if not config:
        raise HTTPException(status_code=404, detail="WeCom not configured")

    config_out = ChannelConfigOut.model_validate(config)
    if (config.extra_config or {}).get("connection_mode") == "websocket":
        config_out.is_connected = wecom_stream_manager.status().get(str(agent_id), False)
    else:
        config_out.is_connected = False
    return config_out


@router.get("/agents/{agent_id}/wecom-channel/webhook-url")
async def get_wecom_webhook_url(agent_id: uuid.UUID, request: Request, db=None):
    public_base = await platform_service.get_public_base_url(db, request)
    return {"webhook_url": f"{public_base}/api/channel/wecom/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/wecom-channel", status_code=204)
async def delete_wecom_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="wecom")
    if not config:
        raise HTTPException(status_code=404, detail="WeCom not configured")
    await wecom_stream_manager.stop_client(agent_id)
    await channel_config_dao.delete(id=config.id)


# ─── Event Webhook ──────────────────────────────────────


@router.get("/channel/wecom/{agent_id}/webhook")
async def wecom_verify_webhook(
    agent_id: uuid.UUID, msg_signature: str = "", timestamp: str = "", nonce: str = "", echostr: str = ""
):
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
):
    """Handle WeCom message callback (POST request with encrypted XML)."""
    body_bytes = await request.body()

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

    # Dedup
    from app.services.channels import dedup as channel_dedup

    dedup_key = msg_id if msg_id else token
    if dedup_key and await channel_dedup.already_processed_shared("wecom", dedup_key):
        return Response(content="success", media_type="text/plain")
    if dedup_key:
        await channel_dedup.mark_processed_shared("wecom", dedup_key)

    logger.info(f"[WeCom] Message type={msg_type}, from={from_user}, msg_id={msg_id}, chat_id={chat_id or 'N/A'}")

    if msg_type == "text":
        user_text = msg_root.findtext("Content", "").strip()
        if not user_text:
            return Response(content="success", media_type="text/plain")

        # Process in background task (manages its own sessions)
        _schedule_background(_process_wecom_text(agent_id, config, from_user, user_text, chat_id=chat_id))

    elif msg_type == "event":
        event = msg_root.findtext("Event", "")
        if event == "kf_msg_or_event":
            _schedule_background(_process_wecom_kf_event(agent_id, config, token, open_kfid))
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
    """Sync WeCom Customer Service (KF) messages in background."""
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
            token_data = tok_resp.json()
            access_token = token_data.get("access_token")
            if not access_token:
                return

            current_cursor = token
            has_more = 1
            current_ts = int(time.time())

            while has_more:
                payload: dict[str, int | str] = {"limit": 20}
                if open_kfid:
                    payload["open_kfid"] = open_kfid

                if current_cursor.startswith("ENC"):
                    payload["token"] = current_cursor
                else:
                    payload["cursor"] = current_cursor

                logger.info(f"[WeCom KF] Calling sync_msg with payload: {payload}")
                sync_resp = await client.post(
                    f"https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg?access_token={access_token}", json=payload
                )
                sync_data = sync_resp.json()
                if sync_data.get("errcode") != 0:
                    logger.error(f"[WeCom KF] sync_msg error: {sync_data}")
                    break

                has_more = sync_data.get("has_more", 0)
                current_cursor = sync_data.get("next_cursor", "")

                for msg in sync_data.get("msg_list", []):
                    if msg.get("origin") == 3 and msg.get("msgtype") == "text":
                        mid = msg.get("msgid")
                        from app.services.channels import dedup as channel_dedup

                        if mid and await channel_dedup.already_processed_shared("wecom_kf", str(mid)):
                            continue
                        if msg.get("send_time", 0) > 0 and (current_ts - msg.get("send_time", 0) > 86400):
                            continue
                        if mid:
                            await channel_dedup.mark_processed_shared("wecom_kf", str(mid))
                        text = msg.get("text", {}).get("content", "").strip()
                        if text:
                            logger.info(f"[WeCom KF] Found msg from {msg.get('external_userid')}: {text[:20]}...")
                            await _process_wecom_text(
                                agent_id,
                                config,
                                msg.get("external_userid"),
                                text,
                                is_kf=True,
                                open_kfid=msg.get("open_kfid"),
                                kf_msg_id=mid,
                            )
                if not has_more:
                    break
    except Exception as e:
        logger.error(f"[WeCom KF] Error in background task: {e}")


async def _process_wecom_text(
    agent_id: uuid.UUID,
    config: ChannelConfigRecord,
    from_user: str,
    user_text: str,
    is_kf: bool = False,
    open_kfid: str | None = None,
    kf_msg_id: str | None = None,
    chat_id: str = "",
):
    """Process an incoming WeCom text message and reply.

    Manages its own short-lived database transactions via pure-psycopg DAOs.
    """
    _ = kf_msg_id
    agent_obj = await agent_dao.get(agent_id)
    if not agent_obj:
        logger.warning(f"[WeCom] Agent {agent_id} not found")
        return
    creator_id = agent_obj.creator_id
    ctx_size = agent_obj.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE

    # Distinguish group chat from P2P by chat_id presence
    _is_group = bool(chat_id)
    conv_id = f"wecom_group_{chat_id}" if _is_group else f"wecom_p2p_{from_user}"

    # The channel_user_service resolves display names from OrgMember records
    # (populated by org-sync or enriched on first SSO login). No need to
    # make an extra API call here - it fails with 48009 when IP is not whitelisted.
    extra_info = {"unionid": from_user}

    # Resolve channel user via unified service (uses OrgMember + SSO patterns)
    platform_user = await channel_user_service.resolve_channel_user(
        db=None,
        agent=agent_obj,
        channel_type="wecom",
        external_user_id=from_user,
        extra_info=extra_info,
    )
    platform_user_id = platform_user.id

    # Find or create session
    sess = await find_or_create_channel_session(
        db=None,
        agent_id=agent_id,
        user_id=creator_id if _is_group else platform_user_id,
        external_conv_id=conv_id,
        source_channel="wecom",
        first_message_title=user_text,
        is_group=_is_group,
        group_name=f"WeCom Group {chat_id[:8]}" if _is_group else None,
    )
    session_conv_id = str(sess.id)

    from app.services.llm.utils import convert_chat_messages_to_llm_format as _conv

    history_msgs = await chat_message_dao.list_recent(
        agent_id=agent_id,
        conversation_id=session_conv_id,
        limit=ctx_size,
    )
    history = _conv(history_msgs)

    await chat_message_dao.insert_message(
        agent_id=agent_id,
        user_id=platform_user_id,
        role="user",
        content=user_text,
        conversation_id=session_conv_id,
    )
    await chat_session_dao.update(db_obj=sess, obj_in={"last_message_at": datetime.now(UTC)})

    from app.api.feishu import _load_agent_and_model

    _agent_model, _llm_model, _fallback_model = await _load_agent_and_model(None, agent_id)

    from app.api.feishu import _call_llm_with_config

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
    logger.info(f"[WeCom] LLM reply: {reply_text[:100]}")

    # Send reply via WeCom API
    wecom_agent_id = (config.extra_config or {}).get("wecom_agent_id", "")
    wecom_agent_id_str = wecom_agent_id if isinstance(wecom_agent_id, str) else ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            tok_resp = await client.get(
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                params={"corpid": config.app_id, "corpsecret": config.app_secret},
            )
            access_token = tok_resp.json().get("access_token", "")
            if access_token:
                if is_kf and open_kfid:
                    # For KF messages, need to bridge/trans state first then send via kf/send_msg
                    res_state = await client.post(
                        f"https://qyapi.weixin.qq.com/cgi-bin/kf/service_state/trans?access_token={access_token}",
                        json={"open_kfid": open_kfid, "external_userid": from_user, "service_state": 1},
                    )
                    logger.info(f"[WeCom KF] trans state result: {res_state.json()}")
                    res_send = await client.post(
                        f"https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg?access_token={access_token}",
                        json={
                            "touser": from_user,
                            "open_kfid": open_kfid,
                            "msgtype": "text",
                            "text": {"content": reply_text},
                        },
                    )
                    logger.info(f"[WeCom KF] send_msg result: {res_send.json()}")
                else:
                    # Default legacy Send as text
                    await client.post(
                        f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}",
                        json={
                            "touser": from_user,
                            "msgtype": "text",
                            "agentid": int(wecom_agent_id_str) if wecom_agent_id_str else 0,
                            "text": {"content": reply_text},
                        },
                    )
    except Exception as e:
        logger.error(f"[WeCom] Failed to send reply: {e}")

    await chat_message_dao.insert_message(
        agent_id=agent_id,
        user_id=platform_user_id,
        role="assistant",
        content=reply_text,
        conversation_id=session_conv_id,
    )
    try:
        fresh = await chat_session_dao.get(uuid.UUID(session_conv_id))
        if fresh:
            await chat_session_dao.update(db_obj=fresh, obj_in={"last_message_at": datetime.now(UTC)})
    except ValueError, TypeError:
        pass

    await log_activity(
        agent_id,
        "chat_reply",
        f"Replied to WeCom message: {reply_text[:80]}",
        detail={"channel": "wecom", "user_text": user_text[:200], "reply": reply_text[:500]},
    )


# ─── OAuth Callback (SSO) ──────────────────────────────


@router.get("/auth/wecom/callback")
async def wecom_callback(code: str, state: str | None = None):
    # 1. Resolve session to get tenant context
    tenant_id = None
    if state:
        try:
            sid = uuid.UUID(state)
            session = await sso_scan_session_dao.get(sid)
            if session:
                tenant_id = session.tenant_id
        except ValueError, AttributeError:
            pass

    # 1. Get WeCom provider config
    provider = await identity_provider_dao.get_by_type_and_tenant("wecom", tenant_id)
    if not provider:
        raise HTTPException(status_code=404, detail="WeCom provider not configured for this tenant")

    # 2. Extract user info and login/register via RegistrationService
    try:
        provider_tenant_id = str(tenant_id) if tenant_id else (str(provider.tenant_id) if provider.tenant_id else None)
        auth_provider = await auth_provider_registry.get_provider("wecom", provider_tenant_id)
        if not auth_provider:
            return HTMLResponse("Auth failed: WeCom provider unavailable")

        token_data = await auth_provider.exchange_code_for_token(code)
        access_token_str = token_data.get("access_token")
        if not isinstance(access_token_str, str) or not access_token_str:
            return HTMLResponse("Auth failed: Token error")

        user_info = await auth_provider.get_user_info(access_token_str)
        if not user_info.provider_user_id:
            return HTMLResponse("Auth failed: No UserId returned")

        # Find or Create User (handles Identity and OrgMember linking)
        user, _is_new = await auth_provider.find_or_create_user(user_info, tenant_id=provider_tenant_id)
    except Exception as e:
        logger.exception(f"WeCom login/register error: {e}")
        return HTMLResponse(f"Auth failed: {e!s}")

    # Standard login
    token = create_access_token(str(user.id), user.role)

    if state:
        try:
            sid = uuid.UUID(state)
            session = await sso_scan_session_dao.get(sid)
            if session:
                await sso_scan_session_dao.update(
                    db_obj=session,
                    obj_in={
                        "status": "authorized",
                        "provider_type": "wecom",
                        "user_id": user.id,
                        "access_token": token,
                        "error_msg": None,
                    },
                )
                return HTMLResponse(
                    f"""<html><head><meta charset="utf-8" /></head>
                    <body style="font-family: sans-serif; padding: 24px;">
                        <div>SSO login successful. Redirecting...</div>
                        <script>window.location.href = "/sso/entry?sid={sid}&complete=1";</script>
                    </body></html>"""
                )
        except Exception as e:
            logger.exception("Failed to update SSO session (wecom) %s", e)

    return HTMLResponse(f"Logged in. Token: {token}")
