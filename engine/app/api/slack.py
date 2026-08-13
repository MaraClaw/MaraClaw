"""Slack Bot Channel API routes."""

import hashlib
import hmac
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.core.logging import logger
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.dao.agent_dao import agent_dao
from app.dao.channel_config_dao import channel_config_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.user_dao import user_dao
from app.records.user import UserRecord
from app.schemas.schemas import ChannelConfigOut
from app.services.channel_session import find_or_create_channel_session
from app.services.channel_user_service import channel_user_service
from app.services.storage import store_agent_upload

router = APIRouter(tags=["slack"])

SLACK_MSG_LIMIT = 4000  # Slack text message char limit
DEFAULT_CONTEXT_WINDOW_SIZE = 100


class SlackChannelPayload(TypedDict, total=False):
    bot_token: str
    signing_secret: str


# ─── Config CRUD ────────────────────────────────────────


@router.post("/agents/{agent_id}/slack-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_slack_channel(
    agent_id: uuid.UUID, data: SlackChannelPayload, current_user: UserRecord = Depends(get_current_user)
):
    """Configure Slack bot for an agent. Fields: bot_token, signing_secret."""
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    bot_token = data.get("bot_token", "").strip()
    signing_secret = data.get("signing_secret", "").strip()
    if not bot_token or not signing_secret:
        raise HTTPException(status_code=422, detail="bot_token and signing_secret are required")

    existing = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="slack")
    if existing:
        config = await channel_config_dao.update(
            db_obj=existing,
            obj_in={
                "app_secret": bot_token,
                "encrypt_key": signing_secret,
                "is_configured": True,
            },
        )
        return ChannelConfigOut.model_validate(config or existing)

    config = await channel_config_dao.create(
        obj_in={
            "agent_id": agent_id,
            "channel_type": "slack",
            "app_id": "slack",
            "app_secret": bot_token,
            "encrypt_key": signing_secret,
            "is_configured": True,
        }
    )
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/slack-channel", response_model=ChannelConfigOut)
async def get_slack_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    await check_agent_access(current_user, agent_id)
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="slack")
    if not config:
        raise HTTPException(status_code=404, detail="Slack not configured")
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/slack-channel/webhook-url")
async def get_slack_webhook_url(agent_id: uuid.UUID, request: Request, db=None):
    from app.services.platform_service import platform_service

    public_base = await platform_service.get_public_base_url(db, request)
    return {"webhook_url": f"{public_base}/api/channel/slack/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/slack-channel", status_code=204)
async def delete_slack_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="slack")
    if not config:
        raise HTTPException(status_code=404, detail="Slack not configured")
    await channel_config_dao.delete(id=config.id)


# ─── Event Webhook ──────────────────────────────────────

_processed_slack_events: set[str] = set()


def _verify_slack_signature(signing_secret: str, body: bytes, headers: Mapping[str, str]) -> bool:
    """Verify Slack's HMAC-SHA256 request signature."""
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    if not ts or not sig:
        return False
    if abs(time.time() - int(ts)) > 300:
        return False
    base = f"v0:{ts}:{body.decode()}"
    expected = "v0=" + hmac.new(signing_secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


async def _send_slack_messages(bot_token: str, channel: str, text: str) -> None:
    """Send text to Slack, splitting into SLACK_MSG_LIMIT chunks if needed."""
    import httpx

    chunks = [text[i : i + SLACK_MSG_LIMIT] for i in range(0, len(text), SLACK_MSG_LIMIT)]
    async with httpx.AsyncClient(timeout=10) as client:
        for chunk in chunks:
            await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
                json={"channel": channel, "text": chunk},
            )


@router.post("/channel/slack/{agent_id}/webhook")
async def slack_event_webhook(agent_id: uuid.UUID, request: Request):
    """Handle Slack Event API callbacks."""
    body_bytes = await request.body()

    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="slack")
    if not config:
        return Response(status_code=404)

    signing_secret = config.encrypt_key or ""
    if signing_secret and not _verify_slack_signature(signing_secret, body_bytes, dict(request.headers)):
        return Response(status_code=401)

    import json

    body = json.loads(body_bytes)
    logger.info(f"[Slack] Webhook for {agent_id}: type={body.get('type')}")

    if body.get("type") == "url_verification":
        return {"challenge": body["challenge"]}

    if body.get("type") != "event_callback":
        return {"ok": True}

    event = body.get("event", {})
    event_id = body.get("event_id", "")

    if event_id in _processed_slack_events:
        return {"ok": True}
    if event_id:
        _processed_slack_events.add(event_id)
        if len(_processed_slack_events) > 1000:
            _processed_slack_events.clear()

    if event.get("bot_id") or event.get("subtype"):
        return {"ok": True}

    event_type = event.get("type", "")
    if event_type not in ("message", "app_mention"):
        return {"ok": True}

    user_text = event.get("text", "").strip()
    import re

    user_text = re.sub(r"^<@[A-Z0-9]+>\s*", "", user_text).strip()

    slack_files = event.get("files", [])

    if not user_text and not slack_files:
        return {"ok": True}

    channel_id = event.get("channel", "")
    sender_id = event.get("user", "")
    _is_group_slack = bool(channel_id) and not channel_id.startswith("D")
    conv_id = f"slack_{channel_id}" if channel_id else f"slack_dm_{sender_id}"

    logger.info(f"[Slack] Message from={sender_id}, channel={channel_id}: {user_text[:80]}")

    from app.api.feishu import _call_llm_with_config, _load_agent_and_model
    from app.services.llm.utils import convert_chat_messages_to_llm_format as _conv

    agent_obj = await agent_dao.get(agent_id)
    if not agent_obj:
        logger.warning(f"[Slack] Agent {agent_id} not found")
        return {"ok": True}
    creator_id = agent_obj.creator_id
    ctx_size = agent_obj.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE

    _bot_token_for_info = config.app_secret or ""
    _slack_real_name = ""
    _slack_email = ""
    _slack_avatar = ""
    if _bot_token_for_info and sender_id:
        try:
            import httpx as _httpx_info

            async with _httpx_info.AsyncClient(timeout=5) as _info_client:
                _info_resp = await _info_client.get(
                    "https://slack.com/api/users.info",
                    headers={"Authorization": f"Bearer {_bot_token_for_info}"},
                    params={"user": sender_id},
                )
                _info_data = _info_resp.json()
                if _info_data.get("ok"):
                    _profile = _info_data.get("user", {}).get("profile", {})
                    _slack_real_name = (
                        _profile.get("display_name")
                        or _profile.get("real_name")
                        or _info_data.get("user", {}).get("real_name")
                        or ""
                    )
                    _slack_email = _profile.get("email", "")
                    _slack_avatar = (
                        _profile.get("image_512") or _profile.get("image_original") or _profile.get("image_192") or ""
                    )
        except Exception as _e_info:
            logger.error(f"[Slack] Failed to fetch user info for {sender_id}: {_e_info}")

    _extra_info = {
        "name": _slack_real_name or f"Slack User {sender_id[:8]}",
        "email": _slack_email,
        "avatar_url": _slack_avatar,
    }
    platform_user = await channel_user_service.resolve_channel_user(
        db=None,
        agent=agent_obj,
        channel_type="slack",
        external_user_id=sender_id,
        extra_info=_extra_info,
    )

    if _slack_real_name and platform_user.display_name and platform_user.display_name.startswith("Slack User "):
        platform_user = (
            await user_dao.update(db_obj=platform_user, obj_in={"display_name": _slack_real_name}) or platform_user
        )
    platform_user_id = platform_user.id

    sess = await find_or_create_channel_session(
        db=None,
        agent_id=agent_id,
        user_id=creator_id if _is_group_slack else platform_user_id,
        external_conv_id=conv_id,
        source_channel="slack",
        first_message_title=user_text,
        is_group=_is_group_slack,
        group_name=f"Slack Channel {channel_id[:8]}" if _is_group_slack else None,
    )
    session_conv_id = str(sess.id)

    history_msgs = await chat_message_dao.list_recent(
        agent_id=agent_id,
        conversation_id=session_conv_id,
        limit=ctx_size,
    )
    history = _conv(history_msgs)

    import asyncio as _asyncio
    import random as _random

    import httpx as _httpx

    from app.api.feishu import _FILE_ACK_MESSAGES

    _file_user_messages = []
    _bot_token = config.app_secret or ""
    for _sf in slack_files:
        _fname = _sf.get("name") or _sf.get("title") or f"slack_file_{_sf.get('id', 'unk')}.bin"
        _url = _sf.get("url_private_download") or _sf.get("url_private", "")
        if not _url:
            continue
        try:
            async with _httpx.AsyncClient(timeout=30, follow_redirects=True) as _hc:
                _r = await _hc.get(_url, headers={"Authorization": f"Bearer {_bot_token}"})
                _r.raise_for_status()
                _ct = _r.headers.get("content-type", "")
                if "text/html" in _ct or _r.content[:15].lower().startswith(b"<!doctype html"):
                    raise ValueError(
                        f"Got HTML response (SSO redirect) - Slack App needs 'files:read' scope. Content-Type: {_ct}"
                    )
                _, _workspace_path, _ = await store_agent_upload(
                    agent_id,
                    _fname,
                    _r.content,
                    content_type=_ct or None,
                )
            _file_user_messages.append(_workspace_path)
            logger.info(f"[Slack] Saved file {_fname} ({len(_r.content)} bytes)")
        except Exception as _e:
            logger.error(f"[Slack] Failed to download file {_fname}: {_e}")

    if not user_text and not _file_user_messages and slack_files:
        _file_names = ", ".join(_sf.get("name", "file") for _sf in slack_files)
        _ack = (
            f"I received the file(s) {_file_names}, but I cannot download their content yet. "
            "Please verify that the Slack app has the files:read permission."
        )
        await chat_message_dao.insert_message(
            agent_id=agent_id,
            user_id=platform_user_id,
            role="assistant",
            content=_ack,
            conversation_id=session_conv_id,
        )
        await chat_session_dao.update(db_obj=sess, obj_in={"last_message_at": datetime.now(UTC)})
        if _bot_token and channel_id:
            await _send_slack_messages(_bot_token, channel_id, _ack)
        return {"ok": True}

    if _file_user_messages and not user_text:
        _file_content = " ".join(f"[file:{p.split('/')[-1]}]" for p in _file_user_messages)
        await chat_message_dao.insert_message(
            agent_id=agent_id,
            user_id=platform_user_id,
            role="user",
            content=_file_content,
            conversation_id=session_conv_id,
        )
        _random_source = _random.SystemRandom()
        await _asyncio.sleep(_random_source.uniform(1.0, 2.0))
        _ack = _random_source.choice(_FILE_ACK_MESSAGES)
        await chat_message_dao.insert_message(
            agent_id=agent_id,
            user_id=platform_user_id,
            role="assistant",
            content=_ack,
            conversation_id=session_conv_id,
        )
        await chat_session_dao.update(db_obj=sess, obj_in={"last_message_at": datetime.now(UTC)})
        if _bot_token and channel_id:
            await _send_slack_messages(_bot_token, channel_id, _ack)
        return {"ok": True}

    if _file_user_messages and user_text:
        user_text += "\n" + " ".join(f"[file:{p.split('/')[-1]}]" for p in _file_user_messages)

    await chat_message_dao.insert_message(
        agent_id=agent_id,
        user_id=platform_user_id,
        role="user",
        content=user_text,
        conversation_id=session_conv_id,
    )
    await chat_session_dao.update(db_obj=sess, obj_in={"last_message_at": datetime.now(UTC)})

    _agent_model, _llm_model, _fallback_model = await _load_agent_and_model(None, agent_id)
    _cfg_app_secret = config.app_secret or ""

    from app.services.agent_tool_exec.channel_context import channel_file_sender as _cfs_s

    async def _slack_file_sender(file_path, msg: str = ""):
        _fp = Path(file_path)
        if not _bot_token or not channel_id:
            return
        _file_stat = await _asyncio.to_thread(_fp.stat)
        _file_bytes = await _asyncio.to_thread(_fp.read_bytes)
        async with _httpx.AsyncClient(timeout=60) as _hc:
            _upload_url_resp = await _hc.post(
                "https://slack.com/api/files.getUploadURLExternal",
                headers={"Authorization": f"Bearer {_bot_token}"},
                data={"filename": _fp.name, "length": str(_file_stat.st_size)},
            )
            _ud = _upload_url_resp.json()
            if not _ud.get("ok"):
                raise RuntimeError(f"Slack upload URL error: {_ud}")
            _upload_url = _ud["upload_url"]
            _file_id = _ud["file_id"]
            await _hc.post(_upload_url, content=_file_bytes, headers={"Content-Type": "application/octet-stream"})
            _complete = await _hc.post(
                "https://slack.com/api/files.completeUploadExternal",
                headers={"Authorization": f"Bearer {_bot_token}"},
                json={"files": [{"id": _file_id}], "channel_id": channel_id, "initial_comment": msg or ""},
            )
            if not _complete.json().get("ok"):
                raise RuntimeError(f"Slack upload complete error: {_complete.json()}")

    _cfs_s_token = _cfs_s.set(_slack_file_sender)

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
    _cfs_s.reset(_cfs_s_token)
    logger.info(f"[Slack] LLM reply: {reply_text[:80]}")

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

    if _cfg_app_secret and channel_id:
        try:
            await _send_slack_messages(_cfg_app_secret, channel_id, reply_text)
        except Exception as e:
            logger.error(f"[Slack] Failed to send: {e}")

    return {"ok": True}
