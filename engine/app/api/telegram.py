"""Creator configuration with automatic registration and authenticated Telegram webhooks."""

import hmac
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import override
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import ValidationError

from app.config import get_settings
from app.core.domain.channels import ChannelType
from app.core.request_body import CHANNEL_WEBHOOK_MAX_BODY_SIZE, read_bounded_body
from app.core.security import get_current_user
from app.dao.channel_config_dao import channel_config_dao
from app.records.user import UserRecord
from app.schemas.channel_response_contracts import WebhookUrlResponse
from app.schemas.content_response_catalog import OkAcknowledgement
from app.schemas.schemas import ChannelConfigOut
from app.schemas.telegram import TelegramChannelConfigUpdate, TelegramUpdate, TelegramWebhookReply
from app.services.agent_setup_lock import setup_workspace_lock
from app.services.channel_user_service import ChannelUserResolutionError
from app.services.channels.config import (
    delete_channel_config,
    require_channel_config_metadata,
    require_channel_creator,
)
from app.services.channels.redact import channel_config_out
from app.services.channels.sender_links import consume_link_command, parse_link_command
from app.services.channels.submissions import ChannelEventConflictError, accept_channel_event, claim_channel_event
from app.services.channels.telegram import telegram_event
from app.services.channels.telegram_setup import configure_telegram, telegram_webhook_url


class _SecretSafeRoute(APIRoute):
    @override
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        handler = super().get_route_handler()

        async def secret_safe(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                # FastAPI's default errors include the rejected credential input.
                raise HTTPException(422, "Invalid Telegram request") from None

        return secret_safe


router = APIRouter(tags=["telegram"], route_class=_SecretSafeRoute)


@router.post("/agents/{agent_id}/telegram-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_telegram_channel(
    agent_id: UUID, data: TelegramChannelConfigUpdate, current_user: UserRecord = Depends(get_current_user)
) -> ChannelConfigOut:
    _ = await require_channel_creator(current_user, agent_id)
    async with setup_workspace_lock(Path(get_settings().AGENT_DATA_DIR) / f"telegram-{agent_id}"):
        _ = await require_channel_creator(current_user, agent_id)
        return channel_config_out(await configure_telegram(agent_id, data))


@router.get("/agents/{agent_id}/telegram-channel", response_model=ChannelConfigOut)
async def get_telegram_channel(
    agent_id: UUID, current_user: UserRecord = Depends(get_current_user)
) -> ChannelConfigOut:
    _ = await require_channel_creator(current_user, agent_id)
    return channel_config_out(await require_channel_config_metadata(agent_id, ChannelType.TELEGRAM))


@router.get("/agents/{agent_id}/telegram-channel/webhook-url", response_model=None)
async def get_telegram_webhook_url(
    agent_id: UUID, current_user: UserRecord = Depends(get_current_user)
) -> WebhookUrlResponse:
    _ = await require_channel_creator(current_user, agent_id)
    config = await require_channel_config_metadata(agent_id, ChannelType.TELEGRAM)
    return {"webhook_url": await telegram_webhook_url(config.id)}


@router.delete("/agents/{agent_id}/telegram-channel", status_code=204, response_model=None)
async def delete_telegram_channel(agent_id: UUID, current_user: UserRecord = Depends(get_current_user)) -> None:
    _ = await require_channel_creator(current_user, agent_id)
    async with setup_workspace_lock(Path(get_settings().AGENT_DATA_DIR) / f"telegram-{agent_id}"):
        _ = await require_channel_creator(current_user, agent_id)
        await delete_channel_config(agent_id, ChannelType.TELEGRAM)


@router.post("/channel/telegram/{config_id}/webhook", response_model=None)
async def telegram_event_webhook(config_id: UUID, request: Request) -> OkAcknowledgement | TelegramWebhookReply:
    config = await channel_config_dao.get(config_id)
    if config is None or config.channel_type != ChannelType.TELEGRAM or not config.is_configured:
        raise HTTPException(404, "Telegram not configured")
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    secret = config.verification_token
    if secret in (None, "") or not supplied or not hmac.compare_digest(supplied.encode(), secret.encode()):
        raise HTTPException(401, "Invalid Telegram webhook secret")
    body = await read_bounded_body(request, CHANNEL_WEBHOOK_MAX_BODY_SIZE)
    if body is None:
        raise HTTPException(413, "Telegram update is too large")
    try:
        update = TelegramUpdate.model_validate_json(body)
    except ValidationError:
        raise HTTPException(400, "Invalid Telegram update") from None
    event = telegram_event(config, update)
    if event is not None:
        command = parse_link_command("telegram", event.text)
        if command is not None:
            if command.code is None:
                text = "Create a Telegram link code in the web application, then send /link CODE here."
            else:
                result = await consume_link_command(event, command.code)
                text = {
                    "linked": "Telegram linked. You can now message this agent.",
                    "already_linked": "This Telegram sender is already linked to your membership.",
                    "invalid": "The link code is invalid or expired. Create a new code in the web application.",
                    "conflict": "This sender or message is already linked. Unlink it in the web application first.",
                    "replayed": "This link code or message was already used. Check your link in the web application.",
                }[result]
            return {"method": "sendMessage", "chat_id": event.conversation_id.removeprefix("telegram_"), "text": text}
        try:
            if event.media_ids and update.message is not None:
                from app.services.channels.submissions import is_channel_event_replay
                from app.services.channels.telegram_media import enrich_media

                async with claim_channel_event(event):
                    if not await is_channel_event_replay(event):
                        enriched, content = await enrich_media(config, event, update.message)
                        _ = await accept_channel_event(enriched, content=content, saved_content=content)
            else:
                _ = await accept_channel_event(event)
        except ChannelUserResolutionError:
            return {
                "method": "sendMessage",
                "chat_id": event.conversation_id.removeprefix("telegram_"),
                "text": "Create a Telegram link code in the web application, then send /link CODE here.",
            }
        except ChannelEventConflictError:
            return {"ok": True}
    return {"ok": True}
