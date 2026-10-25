"""DingTalk Channel API routes.

Provides Config CRUD and message handling for DingTalk bots using Stream mode.
"""

import json
import uuid
from typing import TypedDict

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

from app.core.domain.channels import ChannelType
from app.core.logging import logger
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.dao.channel_config_dao import channel_config_dao
from app.records.user import UserRecord
from app.schemas.schemas import ChannelConfigOut
from app.services.channels.submissions import ChannelEvent, accept_channel_event
from app.services.sso_scan_sessions import authorize_scan_session, load_authorizable_session, parse_scan_state

router = APIRouter(tags=["dingtalk"])


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

    existing = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.DINGTALK)
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
    _ = await check_agent_access(current_user, agent_id)
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.DINGTALK)
    if not config:
        raise HTTPException(status_code=404, detail="DingTalk not configured")
    return ChannelConfigOut.model_validate(config)


@router.delete("/agents/{agent_id}/dingtalk-channel", status_code=204)
async def delete_dingtalk_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.DINGTALK)
    if not config:
        raise HTTPException(status_code=404, detail="DingTalk not configured")
    _ = await channel_config_dao.delete(record_id=config.id)

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
    native_event: ChannelEvent | None = None,
):
    import re as _re_dt

    _ = sender_nick, session_webhook
    sender_staff_id = (sender_staff_id or "").strip()
    if not message_id and native_event is None:
        raise HTTPException(status_code=400, detail="DingTalk message ID is required")
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.DINGTALK)
    if config is None:
        raise HTTPException(status_code=404, detail="DingTalk not configured")
    native = native_event or ChannelEvent(
        config_id=config.id,
        agent_id=agent_id,
        provider="dingtalk",
        event_key=json.dumps([config.app_id, conversation_id, message_id], separators=(",", ":")),
        sender_id=sender_staff_id,
        conversation_id=(
            f"dingtalk_group_{conversation_id}" if conversation_type == "2" else f"dingtalk_p2p_{sender_staff_id}"
        ),
        text=user_text,
        is_group=conversation_type == "2",
    )
    saved = _re_dt.sub(r"\[image_data:data:image/[^;]+;base64,[A-Za-z0-9+/=]+\]", "", user_text).strip()
    if saved_file_paths is not None and len(saved_file_paths) > 0:
        saved = "\n".join([*(f"[file:{path}]" for path in saved_file_paths), saved]).strip()
    enriched = user_text
    if image_base64_list is not None and len(image_base64_list) > 0:
        enriched += "\n" + "\n".join(f"[image_data:{uri}]" for uri in image_base64_list)
    await accept_channel_event(native, content=enriched, saved_content=saved)


# ─── OAuth Callback (SSO) ──────────────────────────────


@router.get("/auth/dingtalk/callback")
async def dingtalk_callback(auth_code: str = Query(alias="authCode"), state: str | None = None) -> HTMLResponse:
    """Callback for DingTalk OAuth2 login."""
    from app.services.auth_registry import auth_provider_registry

    sid = parse_scan_state(state)
    if sid is None:
        return HTMLResponse("Auth failed: invalid SSO session")

    try:
        session = await load_authorizable_session(sid)
        if session is None:
            return HTMLResponse("Auth failed: invalid SSO session")
        tenant_id = str(session.tenant_id) if session.tenant_id else None
        auth_provider = await auth_provider_registry.get_provider("dingtalk", tenant_id)
        if not auth_provider:
            return HTMLResponse("Auth failed: DingTalk provider not configured for this tenant")

        token_data = await auth_provider.exchange_code_for_token(auth_code)
        access_token = token_data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            logger.error("DingTalk token exchange failed: missing or invalid access token")
            return HTMLResponse("Auth failed: Token exchange error")

        user_info = await auth_provider.get_user_info(access_token)
        if user_info.provider_union_id in (None, ""):
            logger.error("DingTalk user info missing unionId")
            return HTMLResponse("Auth failed: No unionid returned")

        user, _ = await auth_provider.find_or_create_user(user_info, tenant_id=tenant_id)
        if not user:
            return HTMLResponse("Auth failed: User resolution failed")
        if not await authorize_scan_session(sid, user=user, provider_type="dingtalk"):
            return HTMLResponse("Auth failed: invalid SSO session")

    except Exception:
        logger.error("DingTalk login error")
        return HTMLResponse("Auth failed: Authentication error")

    return HTMLResponse(
        f"""<html><head><meta charset="utf-8" /></head>
        <body style="font-family: sans-serif; padding: 24px;">
            <div>SSO login successful. Redirecting...</div>
            <script>window.location.href = "/sso/entry?sid={sid}&complete=1";</script>
        </body></html>"""
    )
