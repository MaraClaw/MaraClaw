"""DingTalk Channel API routes.

Provides Config CRUD and message handling for DingTalk bots using Stream mode.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.logging import logger
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.dao.agent_dao import agent_dao
from app.dao.channel_config_dao import channel_config_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.sso_scan_session_dao import sso_scan_session_dao
from app.records.user import UserRecord
from app.schemas.schemas import ChannelConfigOut
from app.services.channel_session import find_or_create_channel_session
from app.services.channel_user_service import channel_user_service

router = APIRouter(tags=["dingtalk"])

DEFAULT_CONTEXT_WINDOW_SIZE = 100


class DingTalkExtraConfig(TypedDict, total=False):
    connection_mode: str
    agent_id: str


class DingTalkChannelPayload(TypedDict, total=False):
    app_key: str
    app_secret: str
    extra_config: DingTalkExtraConfig


# ─── Config CRUD ────────────────────────────────────────


@router.post("/agents/{agent_id}/dingtalk-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_dingtalk_channel(
    agent_id: uuid.UUID, data: DingTalkChannelPayload, current_user: UserRecord = Depends(get_current_user)
):
    """Configure DingTalk bot for an agent. Fields: app_key, app_secret, agent_id (optional)."""
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    app_key = data.get("app_key", "").strip()
    app_secret = data.get("app_secret", "").strip()
    if not app_key or not app_secret:
        raise HTTPException(status_code=422, detail="app_key and app_secret are required")

    extra_config = data.get("extra_config") or {}
    conn_mode = extra_config.get("connection_mode", "websocket")
    dingtalk_agent_id = extra_config.get("agent_id", "")

    existing = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="dingtalk")
    if existing:
        config = await channel_config_dao.update(
            db_obj=existing,
            obj_in={
                "app_id": app_key,
                "app_secret": app_secret,
                "is_configured": True,
                "extra_config": {
                    **(existing.extra_config or {}),
                    "connection_mode": conn_mode,
                    "agent_id": dingtalk_agent_id,
                },
            },
        )
        config = config or existing

        from app.api.background_tasks import schedule_background_task
        from app.services.dingtalk_stream import dingtalk_stream_manager

        if conn_mode == "websocket":
            schedule_background_task(
                dingtalk_stream_manager.start_client(agent_id, app_key, app_secret),
                "start DingTalk stream client",
            )
        else:
            schedule_background_task(dingtalk_stream_manager.stop_client(agent_id), "stop DingTalk stream client")

        return ChannelConfigOut.model_validate(config)

    config = await channel_config_dao.create(
        obj_in={
            "agent_id": agent_id,
            "channel_type": "dingtalk",
            "app_id": app_key,
            "app_secret": app_secret,
            "is_configured": True,
            "extra_config": {"connection_mode": conn_mode, "agent_id": dingtalk_agent_id},
        }
    )

    if conn_mode == "websocket":
        from app.api.background_tasks import schedule_background_task
        from app.services.dingtalk_stream import dingtalk_stream_manager

        schedule_background_task(
            dingtalk_stream_manager.start_client(agent_id, app_key, app_secret),
            "start DingTalk stream client",
        )

    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/dingtalk-channel", response_model=ChannelConfigOut)
async def get_dingtalk_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    await check_agent_access(current_user, agent_id)
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="dingtalk")
    if not config:
        raise HTTPException(status_code=404, detail="DingTalk not configured")
    return ChannelConfigOut.model_validate(config)


@router.delete("/agents/{agent_id}/dingtalk-channel", status_code=204)
async def delete_dingtalk_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="dingtalk")
    if not config:
        raise HTTPException(status_code=404, detail="DingTalk not configured")
    await channel_config_dao.delete(id=config.id)

    from app.api.background_tasks import schedule_background_task
    from app.services.dingtalk_stream import dingtalk_stream_manager

    schedule_background_task(dingtalk_stream_manager.stop_client(agent_id), "stop DingTalk stream client")


# ─── Message Processing (called by Stream callback) ────


async def process_dingtalk_message(
    agent_id: uuid.UUID,
    sender_staff_id: str,
    user_text: str,
    conversation_id: str,
    conversation_type: str,
    session_webhook: str,
    image_base64_list: list[str] | None = None,
    saved_file_paths: list[str] | None = None,
    sender_nick: str = "",
    message_id: str = "",
):
    """Process an incoming DingTalk bot message and reply via session webhook."""
    import re as _re_dt

    import httpx

    from app.api.feishu import _call_llm_with_config, _load_agent_and_model
    from app.services.llm.utils import convert_chat_messages_to_llm_format as _conv

    _ = sender_nick
    sender_staff_id = (sender_staff_id or "").strip()

    agent_obj = await agent_dao.get(agent_id)
    if not agent_obj:
        logger.warning(f"[DingTalk] Agent {agent_id} not found")
        return
    if not sender_staff_id:
        logger.warning("[DingTalk] Skip message attribution because sender_staff_id is empty")
        return

    ctx_size = agent_obj.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE
    conv_id = f"dingtalk_group_{conversation_id}" if conversation_type == "2" else f"dingtalk_p2p_{sender_staff_id}"

    platform_user = await channel_user_service.resolve_channel_user(
        db=None,
        agent=agent_obj,
        channel_type="dingtalk",
        external_user_id=sender_staff_id,
        extra_info={},
    )
    platform_user_id = platform_user.id

    sess = await find_or_create_channel_session(
        db=None,
        agent_id=agent_id,
        user_id=platform_user_id,
        external_conv_id=conv_id,
        source_channel="dingtalk",
        first_message_title=user_text,
    )
    session_conv_id = str(sess.id)

    history_msgs = await chat_message_dao.list_recent(
        agent_id=agent_id,
        conversation_id=session_conv_id,
        limit=ctx_size,
    )
    history = _conv(history_msgs)

    _clean_text = _re_dt.sub(
        r"\[image_data:data:image/[^;]+;base64,[A-Za-z0-9+/=]+\]",
        "",
        user_text,
    ).strip()
    if saved_file_paths:
        _file_prefixes = "\n".join(f"[file:{Path(p).name}]" for p in saved_file_paths)
        saved_content = f"{_file_prefixes}\n{_clean_text}".strip() if _clean_text else _file_prefixes
    else:
        saved_content = _clean_text or user_text

    await chat_message_dao.insert_message(
        agent_id=agent_id,
        user_id=platform_user_id,
        role="user",
        content=saved_content,
        conversation_id=session_conv_id,
    )
    await chat_session_dao.update(db_obj=sess, obj_in={"last_message_at": datetime.now(UTC)})

    _dt_cfg = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="dingtalk")
    _dt_app_key = _dt_cfg.app_id if _dt_cfg else None
    _dt_app_secret = _dt_cfg.app_secret if _dt_cfg else None

    _agent_model, _llm_model, _fallback_model = await _load_agent_and_model(None, agent_id)
    _agent_name = agent_obj.name

    llm_user_text = user_text
    if image_base64_list:
        image_markers = "\n".join(f"[image_data:{uri}]" for uri in image_base64_list)
        llm_user_text = f"{user_text}\n{image_markers}" if user_text else image_markers

    from app.services.agent_tool_exec.channel_context import channel_file_sender as _cfs
    from app.services.dingtalk_stream import (
        _send_dingtalk_media_message,
        _upload_dingtalk_media,
    )

    _cfs_token = None
    if _dt_app_key and _dt_app_secret:
        _dt_target_id = conversation_id if conversation_type == "2" else sender_staff_id
        _dt_conv_type = conversation_type

        async def _dingtalk_file_sender(file_path: Path, msg: str = "") -> None:
            """Send a file/image/video via DingTalk proactive message API."""
            _fp = file_path
            _ext = _fp.suffix.lower()

            if _ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"):
                _media_type = "image"
            elif _ext in (".mp4", ".mov", ".avi", ".mkv"):
                _media_type = "video"
            elif _ext in (".mp3", ".wav", ".ogg", ".amr", ".m4a"):
                _media_type = "voice"
            else:
                _media_type = "file"

            _mid = await _upload_dingtalk_media(_dt_app_key, _dt_app_secret, str(file_path), _media_type)

            if _mid:
                _ok = await _send_dingtalk_media_message(
                    _dt_app_key,
                    _dt_app_secret,
                    _dt_target_id,
                    _mid,
                    _media_type,
                    _dt_conv_type,
                    filename=_fp.name,
                )
                if _ok:
                    if msg:
                        try:
                            async with httpx.AsyncClient(timeout=10) as _cl:
                                await _cl.post(
                                    session_webhook,
                                    json={
                                        "msgtype": "text",
                                        "text": {"content": msg},
                                    },
                                )
                        except Exception as accompanying_text_error:
                            logger.warning(
                                f"[DingTalk] Failed to send accompanying file text: {accompanying_text_error}"
                            )
                    return

            _fallback_parts = []
            if msg:
                _fallback_parts.append(msg)
            _fallback_parts.append(f"[File: {_fp.name}]")
            try:
                async with httpx.AsyncClient(timeout=10) as _cl:
                    await _cl.post(
                        session_webhook,
                        json={
                            "msgtype": "text",
                            "text": {"content": "\n\n".join(_fallback_parts)},
                        },
                    )
            except Exception as _fb_err:
                logger.error(f"[DingTalk] Fallback file text also failed: {_fb_err}")

        _cfs_token = _cfs.set(_dingtalk_file_sender)

    try:
        reply_text = await _call_llm_with_config(
            _agent_model,
            _llm_model,
            _fallback_model,
            agent_id,
            llm_user_text,
            history=history,
            user_id=platform_user_id,
        )
    finally:
        if _cfs_token is not None:
            _cfs.reset(_cfs_token)
        if message_id and _dt_app_key and _dt_app_secret:
            try:
                from app.services.dingtalk_reaction import recall_thinking_reaction

                await recall_thinking_reaction(
                    _dt_app_key,
                    _dt_app_secret,
                    message_id,
                    conversation_id,
                )
            except Exception as _recall_err:
                logger.warning(f"[DingTalk] Failed to recall thinking reaction: {_recall_err}")

    has_media = bool(image_base64_list or saved_file_paths)
    logger.info(f"[DingTalk] LLM reply ({'media' if has_media else 'text'} input): {reply_text[:100]}")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                session_webhook,
                json={
                    "msgtype": "markdown",
                    "markdown": {
                        "title": _agent_name or "AI Reply",
                        "text": reply_text,
                    },
                },
            )
    except Exception as e:
        logger.error(f"[DingTalk] Failed to reply via webhook: {e}")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    session_webhook,
                    json={
                        "msgtype": "text",
                        "text": {"content": reply_text},
                    },
                )
        except Exception as e2:
            logger.error(f"[DingTalk] Fallback text reply also failed: {e2}")

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

    from app.services.activity_logger import log_activity

    await log_activity(
        agent_id,
        "chat_reply",
        f"Replied to DingTalk message: {reply_text[:80]}",
        detail={"channel": "dingtalk", "user_text": user_text[:200], "reply": reply_text[:500]},
    )


# ─── OAuth Callback (SSO) ──────────────────────────────


@router.get("/auth/dingtalk/callback")
async def dingtalk_callback(auth_code: str = Query(alias="authCode"), state: str | None = None):
    """Callback for DingTalk OAuth2 login."""
    from fastapi.responses import HTMLResponse

    from app.core.security import create_access_token
    from app.services.auth_registry import auth_provider_registry

    tenant_id = None
    if state:
        try:
            sid = uuid.UUID(state)
            session = await sso_scan_session_dao.get(sid)
            if session:
                tenant_id = session.tenant_id
        except ValueError, AttributeError:
            pass

    auth_provider = await auth_provider_registry.get_provider("dingtalk", str(tenant_id) if tenant_id else None)
    if not auth_provider:
        return HTMLResponse("Auth failed: DingTalk provider not configured for this tenant")

    try:
        token_data = await auth_provider.exchange_code_for_token(auth_code)
        access_token = token_data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            logger.error(f"DingTalk token exchange failed: {token_data}")
            return HTMLResponse("Auth failed: Token exchange error")

        user_info = await auth_provider.get_user_info(access_token)
        if not user_info.provider_union_id:
            logger.error(f"DingTalk user info missing unionId: {user_info.raw_data}")
            return HTMLResponse("Auth failed: No unionid returned")

        user, _ = await auth_provider.find_or_create_user(user_info, tenant_id=str(tenant_id) if tenant_id else None)
        if not user:
            return HTMLResponse("Auth failed: User resolution failed")

    except Exception as e:
        logger.error(f"DingTalk login error: {e}")
        return HTMLResponse(f"Auth failed: {e!s}")

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
                        "provider_type": "dingtalk",
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
            logger.exception("Failed to update SSO session (dingtalk) %s", e)

    return HTMLResponse(f"Logged in. Token: {token}")
