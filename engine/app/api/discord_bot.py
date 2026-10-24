"""Discord Bot Channel API routes (slash command interactions)."""

import json
import os
import uuid
from collections.abc import Mapping
from typing import TypedDict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from app.core.json_types import (
    JsonObject,
    json_as_int,
    json_as_str,
    json_as_str_or,
    json_loads_object,
    json_object_from,
)
from app.core.logging import logger
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.request_body import CHANNEL_WEBHOOK_MAX_BODY_SIZE, read_bounded_body
from app.core.security import get_current_user
from app.dao.channel_config_dao import channel_config_dao
from app.records.user import UserRecord
from app.schemas.channel_response_contracts import DiscordInteractionResponse, WebhookUrlResponse
from app.schemas.schemas import ChannelConfigOut

router = APIRouter(tags=["discord"])

DISCORD_MSG_LIMIT = 2000  # Discord message char limit


class DiscordChannelPayload(TypedDict, total=False):
    connection_mode: str
    bot_token: str
    application_id: str
    public_key: str


class DiscordSlashRegistration(TypedDict):
    status: int
    body: str


# ─── Config CRUD ────────────────────────────────────────


@router.post("/agents/{agent_id}/discord-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_discord_channel(
    agent_id: uuid.UUID, data: DiscordChannelPayload, current_user: UserRecord = Depends(get_current_user)
) -> ChannelConfigOut:
    """Configure Discord bot for an agent.

    Gateway mode fields: bot_token (+ connection_mode='gateway').
    Webhook mode fields: application_id, bot_token, public_key.
    """
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    connection_mode = data.get("connection_mode", "webhook").strip()
    bot_token = data.get("bot_token", "").strip()
    application_id = data.get("application_id", "").strip()
    public_key = data.get("public_key", "").strip()

    if not bot_token:
        raise HTTPException(status_code=422, detail="bot_token is required")
    if connection_mode != "gateway" and (not application_id or not _valid_discord_public_key(public_key)):
        raise HTTPException(
            status_code=422, detail="application_id and a 32-byte hex public_key are required for webhook mode"
        )

    extra_config: JsonObject = {"connection_mode": connection_mode}

    existing = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="discord")
    if existing:
        config = await channel_config_dao.update(
            db_obj=existing,
            obj_in={
                "app_id": application_id or existing.app_id,
                "app_secret": bot_token,
                "encrypt_key": public_key or existing.encrypt_key,
                "extra_config": extra_config,
                "is_configured": True,
            },
        )
        config = config or existing
    else:
        config = await channel_config_dao.create(
            obj_in={
                "agent_id": agent_id,
                "channel_type": "discord",
                "app_id": application_id,
                "app_secret": bot_token,
                "encrypt_key": public_key,
                "extra_config": extra_config,
                "is_configured": True,
            }
        )

    # Mode-specific post-configuration
    if connection_mode == "gateway":
        from app.services.discord_gateway import discord_gateway_manager

        await discord_gateway_manager.start_client(agent_id, bot_token)
    else:
        try:
            reg = await _register_slash_commands(application_id, bot_token)
            logger.info(f"[Discord] Slash command registration: {reg['status']}")
        except Exception as e:
            logger.warning(f"[Discord] Could not register slash commands: {e}")

    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/discord-channel", response_model=ChannelConfigOut)
async def get_discord_channel(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> ChannelConfigOut:
    _ = await check_agent_access(current_user, agent_id)
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="discord")
    if not config:
        raise HTTPException(status_code=404, detail="Discord not configured")
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/discord-channel/webhook-url", response_model=None)
async def get_discord_webhook_url(agent_id: uuid.UUID, request: Request) -> WebhookUrlResponse:
    from app.services.platform_service import platform_service

    public_base = await platform_service.get_public_base_url(request)
    return {"webhook_url": f"{public_base}/api/channel/discord/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/discord-channel", status_code=204, response_model=None)
async def delete_discord_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)) -> None:
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="discord")
    if not config:
        raise HTTPException(status_code=404, detail="Discord not configured")
    try:
        from app.services.discord_gateway import discord_gateway_manager

        await discord_gateway_manager.stop_client(agent_id)
    except Exception as error:
        logger.warning(f"[Discord] Failed to stop Gateway client: {error}")
    _ = await channel_config_dao.delete(id=config.id)


# ─── Slash Command Registration ─────────────────────────


async def _register_slash_commands(application_id: str, bot_token: str) -> DiscordSlashRegistration:
    """Register /ask global slash command with Discord API."""
    import httpx

    command: JsonObject = {
        "name": "ask",
        "description": "Ask the AI agent a question",
        "options": [
            {
                "name": "message",
                "description": "Your question or message to the agent",
                "type": 3,  # STRING
                "required": True,
            }
        ],
    }
    url = f"https://discord.com/api/v10/applications/{application_id}/commands"
    proxy = os.environ.get("DISCORD_PROXY") or os.environ.get("HTTPS_PROXY") or None
    async with httpx.AsyncClient(timeout=15, proxy=proxy) as client:
        resp = await client.put(
            url,
            headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
            json=[command],
        )
        return {"status": resp.status_code, "body": resp.text}


# ─── Interactions Webhook ───────────────────────────────


def _valid_discord_public_key(public_key: str) -> bool:
    try:
        return len(public_key) == 64 and len(bytes.fromhex(public_key)) == 32
    except ValueError:
        return False


def _verify_discord_signature(public_key: str, body: bytes, headers: Mapping[str, str]) -> bool:
    """Verify Discord ed25519 signature."""
    try:
        timestamp = headers.get("x-signature-timestamp", "")
        signature = headers.get("x-signature-ed25519", "")
        if not _valid_discord_public_key(public_key) or not timestamp or len(signature) != 128:
            return False

        verify_key = VerifyKey(bytes.fromhex(public_key))
        _ = verify_key.verify(f"{timestamp}".encode() + body, bytes.fromhex(signature))
        return True
    except BadSignatureError, ValueError:
        return False


async def _send_discord_followup(application_id: str, bot_token: str, interaction_token: str, text: str) -> None:
    """Send follow-up message(s) to Discord Interactions, chunked at 2000 chars."""
    import httpx

    chunks = [text[i : i + DISCORD_MSG_LIMIT] for i in range(0, len(text), DISCORD_MSG_LIMIT)]
    proxy = os.environ.get("DISCORD_PROXY") or os.environ.get("HTTPS_PROXY") or None
    async with httpx.AsyncClient(timeout=10, proxy=proxy) as client:
        for i, chunk in enumerate(chunks):
            if i == 0:
                _ = await client.patch(
                    f"https://discord.com/api/v10/webhooks/{application_id}/{interaction_token}/messages/@original",
                    headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
                    json={"content": chunk},
                )
            else:
                _ = await client.post(
                    f"https://discord.com/api/v10/webhooks/{application_id}/{interaction_token}",
                    headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
                    json={"content": chunk},
                )


@router.post("/channel/discord/{agent_id}/webhook", response_model=None)
async def discord_interaction_webhook(agent_id: uuid.UUID, request: Request) -> Response | DiscordInteractionResponse:
    """Handle Discord Interaction webhooks (PING + slash commands)."""
    body_bytes = await read_bounded_body(request, CHANNEL_WEBHOOK_MAX_BODY_SIZE)
    if body_bytes is None:
        return Response(status_code=413)

    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="discord")
    if not config or (config.extra_config or {}).get("connection_mode") == "gateway":
        return Response(status_code=404)

    public_key = config.encrypt_key or ""
    if not _verify_discord_signature(public_key, body_bytes, request.headers):
        return Response(content="Invalid signature", status_code=401)

    try:
        body = json_loads_object(body_bytes)
    except ValueError, UnicodeError:
        return Response(status_code=401)
    interaction_type = json_as_int(body.get("type"))

    # Type 1: PING - Discord URL verification
    if interaction_type == 1:
        return {"type": 1}

    # Type 2: APPLICATION_COMMAND (slash command)
    if interaction_type == 2:
        data_obj = json_object_from(body.get("data"))
        options_raw = data_obj.get("options")
        user_text = ""
        for raw_opt in options_raw if isinstance(options_raw, list) else []:
            opt = json_object_from(raw_opt)
            if json_as_str(opt.get("name")) == "message":
                user_text = json_as_str_or(opt.get("value")).strip()
                break

        if not user_text:
            return {"type": 4, "data": {"content": "⚠️ Please provide a message. Usage: `/ask message:<your question>`"}}

        member_user = json_object_from(json_object_from(body.get("member")).get("user"))
        user_obj = json_object_from(body.get("user"))
        sender_id = json_as_str(member_user.get("id")) or json_as_str_or(user_obj.get("id"))
        channel_id = json_as_str_or(body.get("channel_id"))
        _is_group_discord = bool(body.get("guild_id"))
        conv_id = f"discord_{channel_id}" if channel_id else f"discord_dm_{sender_id}"

        native_id = json_as_str_or(body.get("id"))
        if not native_id:
            raise HTTPException(status_code=400, detail="Discord interaction ID is required")
        from app.services.channels.submissions import ChannelEvent, accept_channel_event

        await accept_channel_event(
            ChannelEvent(
                config_id=config.id,
                agent_id=agent_id,
                provider="discord",
                event_key=json.dumps([config.app_id, channel_id, native_id], separators=(",", ":")),
                sender_id=sender_id,
                conversation_id=conv_id,
                text=user_text,
                kind="application_command",
                is_group=_is_group_discord,
            )
        )
        return {"type": 5}

    return {"type": 1}
