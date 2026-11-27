"""Feishu OAuth and Channel API routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from app.api.background_tasks import schedule_background_task
from app.config import get_settings
from app.core.domain.channels import ChannelType
from app.core.json_types import JsonObject, json_as_str_or, json_object_from
from app.core.logging import logger
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.request_body import CHANNEL_WEBHOOK_MAX_BODY_SIZE, read_bounded_body
from app.core.security import get_current_user
from app.dao.channel_config_dao import channel_config_dao
from app.process_roles import parse_process_roles, role_enabled
from app.records.user import UserRecord
from app.schemas.channel_response_contracts import WebhookUrlResponse
from app.schemas.schemas import ChannelConfigCreate, ChannelConfigOut
from app.services.channel_user_service import ChannelUserResolutionError
from app.services.channels.config import get_channel_config_metadata
from app.services.channels.redact import channel_config_out
from app.services.channels.stream_source import StreamSource
from app.services.sso_scan_authority import capture_scan_authority
from app.services.sso_scan_sessions import finalize_scan_login, load_authorizable_session, parse_scan_state

router = APIRouter(tags=["feishu"])


# ─── OAuth ──────────────────────────────────────────────


@router.get("/auth/feishu/callback")
async def feishu_oauth_callback(code: str, state: str | None = None) -> HTMLResponse:
    """Handle Feishu OAuth callback - exchange code for user session."""
    sid = parse_scan_state(state)
    if sid is None:
        return HTMLResponse("Auth failed: invalid SSO session")

    try:
        session = await load_authorizable_session(sid)
        if session is None:
            return HTMLResponse("Auth failed: invalid SSO session")
        authority = await capture_scan_authority("feishu", session)
        auth_provider = authority.adapter()

        token_data = await auth_provider.exchange_code_for_token(code)
        access_token = token_data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            logger.error("Feishu token exchange failed: missing or invalid access token")
            return HTMLResponse("Auth failed: Token exchange error")
        user_info = await auth_provider.get_user_info(access_token)

        await finalize_scan_login(authority, user_info)

    except Exception:
        logger.error("Feishu login error")
        return HTMLResponse("Auth failed: Authentication error")

    return HTMLResponse(
        f"""<html><head><meta charset="utf-8" /></head>
        <body style="font-family: sans-serif; padding: 24px;">
            <div>SSO login successful. Redirecting...</div>
            <script>window.location.href = "/sso/entry?sid={sid}&complete=1";</script>
        </body></html>"""
    )


# ─── Channel Config (per-agent Feishu bot) ──────────────


@router.post("/agents/{agent_id}/channel", response_model=ChannelConfigOut, status_code=status.HTTP_201_CREATED)
async def configure_channel(
    agent_id: uuid.UUID, data: ChannelConfigCreate, current_user: UserRecord = Depends(get_current_user)
) -> ChannelConfigOut:
    """Configure Feishu bot credentials for a digital employee (wizard step 5)."""
    agent, _access = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    extra_config = data.extra_config or {}
    if extra_config.get("connection_mode", "webhook") != "websocket" and not (data.verification_token or "").strip():
        raise HTTPException(status_code=422, detail="verification_token is required for webhook mode")
    existing = await get_channel_config_metadata(agent_id, ChannelType.FEISHU)
    if existing:
        config = await channel_config_dao.update(
            db_obj=existing,
            obj_in={
                "app_id": data.app_id,
                "app_secret": data.app_secret,
                "encrypt_key": data.encrypt_key,
                "verification_token": data.verification_token,
                "extra_config": extra_config,
                "is_configured": True,
            },
        )
        config = config or existing
        if role_enabled(parse_process_roles(get_settings().PROCESS_ROLE), "connector"):
            from app.services.feishu_ws import feishu_ws_manager

            mode = json_as_str_or((config.extra_config or {}).get("connection_mode"), "webhook")
            if mode == "websocket":
                schedule_background_task(
                    feishu_ws_manager.start_client(agent_id, data.app_id, data.app_secret), "Feishu start_client"
                )
            else:
                schedule_background_task(feishu_ws_manager.stop_client(agent_id), "Feishu stop_client")

        return channel_config_out(config)

    config = await channel_config_dao.create(
        obj_in={
            "agent_id": agent_id,
            "channel_type": data.channel_type or "feishu",
            "app_id": data.app_id,
            "app_secret": data.app_secret,
            "encrypt_key": data.encrypt_key,
            "verification_token": data.verification_token,
            "extra_config": extra_config,
            "is_configured": True,
        }
    )

    mode = json_as_str_or((config.extra_config or {}).get("connection_mode"), "webhook")
    if mode == "websocket" and role_enabled(parse_process_roles(get_settings().PROCESS_ROLE), "connector"):
        from app.services.feishu_ws import feishu_ws_manager

        schedule_background_task(
            feishu_ws_manager.start_client(agent_id, data.app_id, data.app_secret), "Feishu start_client"
        )

    return channel_config_out(config)


@router.get("/agents/{agent_id}/channel", response_model=ChannelConfigOut)
async def get_channel_config(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> ChannelConfigOut:
    """Get Feishu channel configuration for an agent."""
    _ = await check_agent_access(current_user, agent_id)
    config = await get_channel_config_metadata(agent_id, ChannelType.FEISHU)
    if not config:
        raise HTTPException(status_code=404, detail="Channel not configured")
    return channel_config_out(config)


@router.get("/agents/{agent_id}/channel/webhook-url", response_model=None)
async def get_webhook_url(agent_id: uuid.UUID, request: Request) -> WebhookUrlResponse:
    """Get the webhook URL for this agent's Feishu bot."""
    from app.services.platform_service import platform_service

    public_base = await platform_service.get_public_base_url(request)
    return {"webhook_url": f"{public_base}/api/channel/feishu/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/channel", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_channel_config(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)) -> None:
    """Remove Feishu bot configuration for an agent."""
    agent, _access = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")
    config = await get_channel_config_metadata(agent_id, ChannelType.FEISHU)
    if not config:
        raise HTTPException(status_code=404, detail="Channel not configured")
    _ = await channel_config_dao.delete(record_id=config.id)
    if role_enabled(parse_process_roles(get_settings().PROCESS_ROLE), "connector"):
        from app.services.feishu_ws import feishu_ws_manager

        schedule_background_task(feishu_ws_manager.stop_client(agent_id), "Feishu stop_client")


# ─── Feishu Event Webhook ───────────────────────────────


@router.post("/channel/feishu/{agent_id}/webhook")
async def feishu_event_webhook(
    agent_id: uuid.UUID,
    request: Request,
) -> JsonObject:
    """Handle Feishu event callback for a specific agent's bot."""
    from app.services.channels.feishu_auth import FeishuAuthenticationError, authenticate_feishu_callback

    if await read_bounded_body(request, CHANNEL_WEBHOOK_MAX_BODY_SIZE) is None:
        raise HTTPException(status_code=413, detail="Webhook payload too large")

    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.FEISHU)
    if not config or (config.extra_config or {}).get("connection_mode") == "websocket":
        raise HTTPException(status_code=404, detail="Feishu webhook not configured")
    try:
        body = authenticate_feishu_callback(config, await request.body(), request.headers)
    except FeishuAuthenticationError as error:
        raise HTTPException(status_code=401, detail="Invalid Feishu callback") from error

    # Handle verification challenge
    if body.get("type") == "url_verification" and isinstance(body.get("challenge"), str):
        return {"challenge": body["challenge"]}

    return await process_feishu_event(agent_id, body)


async def process_feishu_event(
    agent_id: uuid.UUID, body: JsonObject, *, source: StreamSource | None = None
) -> JsonObject:
    """Accept an event from the webhook or the WebSocket client; every message goes through native acceptance.

    A message without a message ID is a 400 on the webhook. Over the WebSocket the same refusal would reach
    ``accept_event_frame``, which answers the frame 500 so Feishu redelivers it, and a redelivery has no ID either:
    it is logged and acknowledged instead. A sender who is not a linked member is acknowledged on both paths for the
    same reason, with no reply to the sender.
    """
    from app.services.channels.feishu_submissions import FeishuMessageIdRequiredError, accept_feishu_event
    from app.services.channels.stream_source import authorize_stream_source

    if source is not None:
        await authorize_stream_source(source)

    event_type = json_as_str_or(json_object_from(body.get("header")).get("event_type"))
    logger.info("[Feishu] Event processing for {}: event_type={}", agent_id, event_type or "N/A")
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.FEISHU)
    if not config:
        return {"code": 1, "msg": "Channel not found"}
    if event_type != "im.message.receive_v1":
        return {"code": 0, "msg": "ignored event type"}
    try:
        accepted = await accept_feishu_event(config, json_object_from(body.get("event")), source=source)
    except FeishuMessageIdRequiredError:
        if source is None:
            raise
        logger.warning("[Feishu WS] Ignored a message event without a message ID for agent {}", agent_id)
        return {"code": 0, "msg": "ignored message without ID"}
    except ChannelUserResolutionError:
        logger.info("[Feishu] Ignored a message from a sender who is not a linked member of agent {}", agent_id)
        return {"code": 0, "msg": "ignored unlinked sender"}
    return {"code": 0, "msg": "ok" if accepted else "ignored message type"}
