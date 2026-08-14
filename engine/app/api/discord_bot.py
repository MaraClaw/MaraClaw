"""Discord Bot Channel API routes (slash command interactions)."""

import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TypedDict

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.core.json_types import (
    JsonObject,
    json_as_int,
    json_as_str,
    json_as_str_or,
    json_loads_object,
    json_object_from,
    mapping_from_row,
)
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

router = APIRouter(tags=["discord"])

DISCORD_MSG_LIMIT = 2000  # Discord message char limit
DEFAULT_CONTEXT_WINDOW_SIZE = 100


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
):
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
    if connection_mode == "webhook" and (not application_id or not public_key):
        raise HTTPException(status_code=422, detail="application_id and public_key are required for webhook mode")

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
async def get_discord_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    _ = await check_agent_access(current_user, agent_id)
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="discord")
    if not config:
        raise HTTPException(status_code=404, detail="Discord not configured")
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/discord-channel/webhook-url")
async def get_discord_webhook_url(agent_id: uuid.UUID, request: Request, db: object | None = None):
    from app.services.platform_service import platform_service

    public_base = await platform_service.get_public_base_url(db, request)
    return {"webhook_url": f"{public_base}/api/channel/discord/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/discord-channel", status_code=204)
async def delete_discord_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
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


def _verify_discord_signature(public_key: str, body: bytes, headers: Mapping[str, str]) -> bool:
    """Verify Discord ed25519 signature."""
    try:
        from nacl.signing import VerifyKey

        timestamp = headers.get("x-signature-timestamp", "")
        signature = headers.get("x-signature-ed25519", "")
        if not timestamp or not signature:
            return False

        verify_key = VerifyKey(bytes.fromhex(public_key))
        _ = verify_key.verify(f"{timestamp}".encode() + body, bytes.fromhex(signature))
        return True
    except Exception:
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
async def discord_interaction_webhook(agent_id: uuid.UUID, request: Request) -> Response | JsonObject:
    """Handle Discord Interaction webhooks (PING + slash commands)."""
    body_bytes = await request.body()

    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="discord")
    if not config:
        return Response(status_code=404)

    public_key = config.encrypt_key or ""
    if public_key and not _verify_discord_signature(public_key, body_bytes, dict(request.headers)):
        return Response(content="Invalid signature", status_code=401)

    body = json_loads_object(body_bytes)
    interaction_type = json_as_int(body.get("type"))

    # Type 1: PING - Discord URL verification
    if interaction_type == 1:
        return {"type": 1}

    # Type 2: APPLICATION_COMMAND (slash command)
    if interaction_type == 2:
        data_obj = json_object_from(body.get("data"))
        command_name = json_as_str_or(data_obj.get("name"))
        options_raw = data_obj.get("options")
        user_text = ""
        for raw_opt in options_raw if isinstance(options_raw, list) else []:
            opt = json_object_from(raw_opt)
            if json_as_str(opt.get("name")) == "message":
                user_text = json_as_str_or(opt.get("value")).strip()
                break

        if not user_text:
            return {"type": 4, "data": {"content": "⚠️ Please provide a message. Usage: `/ask message:<your question>`"}}

        interaction_token = json_as_str_or(body.get("token"))
        member_user = json_object_from(json_object_from(body.get("member")).get("user"))
        user_obj = json_object_from(body.get("user"))
        sender_id = json_as_str(member_user.get("id")) or json_as_str_or(user_obj.get("id"))
        channel_id = json_as_str_or(body.get("channel_id"))
        _is_group_discord = bool(body.get("guild_id"))
        conv_id = f"discord_{channel_id}" if channel_id else f"discord_dm_{sender_id}"

        logger.info(f"[Discord] /{command_name} from {sender_id}: {user_text[:80]}")

        async def handle_in_background():
            from app.api.feishu import _call_llm_with_config, _load_agent_and_model
            from app.services.llm.utils import convert_chat_messages_to_llm_format as _conv

            agent_obj = await agent_dao.get(agent_id)
            if not agent_obj:
                logger.warning(f"[Discord] Agent {agent_id} not found")
                return
            creator_id = agent_obj.creator_id
            ctx_size = agent_obj.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE

            _discord_username = json_as_str(member_user.get("username")) or json_as_str_or(user_obj.get("username"))
            _display = _discord_username or f"Discord User {sender_id[:8]}"
            _extra_info = mapping_from_row({"name": _display})

            _platform_user = await channel_user_service.resolve_channel_user(
                db=None,
                agent=agent_obj,
                channel_type="discord",
                external_user_id=sender_id,
                extra_info=_extra_info,
            )

            if (
                _discord_username
                and _platform_user.display_name
                and _platform_user.display_name.startswith("Discord User ")
                and _platform_user.display_name != _discord_username
            ):
                _platform_user = (
                    await user_dao.update(db_obj=_platform_user, obj_in={"display_name": _discord_username})
                    or _platform_user
                )
            platform_user_id = _platform_user.id

            sess = await find_or_create_channel_session(
                db=None,
                agent_id=agent_id,
                user_id=creator_id if _is_group_discord else platform_user_id,
                external_conv_id=conv_id,
                source_channel="discord",
                first_message_title=user_text,
                is_group=_is_group_discord,
                group_name=f"Discord Channel {channel_id[:8]}" if _is_group_discord else None,
            )
            session_conv_id = str(sess.id)

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

            cfg = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="discord")
            _bot_token_bg = cfg.app_secret if cfg else ""
            _app_id_bg = cfg.app_id if cfg else ""

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
            logger.info(f"[Discord] LLM reply: {reply_text[:80]}")

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

            if _bot_token_bg and interaction_token and _app_id_bg:
                try:
                    await _send_discord_followup(_app_id_bg, _bot_token_bg, interaction_token, reply_text)
                except Exception as e:
                    logger.error(f"[Discord] Failed to send follow-up: {e}")

        from app.api.background_tasks import schedule_background_task

        _ = schedule_background_task(handle_in_background(), "handle Discord interaction")
        return {"type": 5}

    return {"type": 1}
