"""Google Chat app channel API routes."""

from __future__ import annotations

import hmac
import json
import uuid
from collections.abc import Callable
from typing import TypedDict

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.core.json_types import JsonValue, json_as_str_or, json_object_from
from app.core.logging import logger
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.request_body import CHANNEL_WEBHOOK_MAX_BODY_SIZE, read_bounded_body
from app.core.security import get_current_user
from app.records.channel_config import ChannelConfigRecord
from app.records.user import UserRecord
from app.schemas.channel_response_contracts import WebhookUrlResponse
from app.schemas.schemas import ChannelConfigOut
from app.services.channel_user_service import ChannelUserResolutionError
from app.services.channels import config as channel_config, google_chat as gchat
from app.services.channels.google_chat_credentials import GoogleChatCredentialError, resolve_google_chat_write
from app.services.channels.redact import channel_config_out
from app.services.platform_service import platform_service

router = APIRouter(tags=["google_chat"])

CHANNEL_TYPE = "google_chat"


def _decode_event_body(body: bytes, load: Callable[[bytes], JsonValue]) -> JsonValue:
    return load(body)


class GoogleChatChannelPayload(TypedDict, total=False):
    """Config payload for a Google Chat app bound to an agent.

    project_number: GCP project number used as JWT audience (required for verify).
    service_account_json: optional SA JSON for proactive / async Chat API sends.
    client_email: optional display / metadata only (do not put PEM in encrypt_key).
    audience: optional JWT audience override.
    verification_token: optional legacy shared token compared to body.token.
    """

    project_number: str
    service_account_json: str | dict[str, object]
    client_email: str
    audience: str
    verification_token: str


@router.post("/agents/{agent_id}/google-chat-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_google_chat_channel(
    agent_id: uuid.UUID,
    data: GoogleChatChannelPayload,
    current_user: UserRecord = Depends(get_current_user),
) -> ChannelConfigOut:
    """Configure Google Chat for an agent."""
    await channel_config.require_channel_creator(current_user, agent_id)

    project_number = str(data.get("project_number") or "").strip()
    if not project_number:
        raise HTTPException(status_code=422, detail="project_number is required")
    verification_token = str(data.get("verification_token") or "").strip() or None

    existing = await channel_config.get_channel_config_metadata(agent_id, CHANNEL_TYPE)
    try:
        write = resolve_google_chat_write(data, existing, project_number)
    except GoogleChatCredentialError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    config = await channel_config.upsert_channel_config(
        agent_id=agent_id,
        channel_type=CHANNEL_TYPE,
        app_id=project_number,
        app_secret=write.client_email,
        encrypt_key=write.encrypt_key,
        verification_token=verification_token,
        extra_config=json_object_from(write.extra_config),
        is_configured=True,
    )
    return channel_config_out(config)


@router.get("/agents/{agent_id}/google-chat-channel", response_model=ChannelConfigOut)
async def get_google_chat_channel(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> ChannelConfigOut:
    agent, _ = await check_agent_access(current_user, agent_id)
    # Secrets: only creator/manage path should see config metadata; still redacted.
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can view channel credentials")
    config = await channel_config.require_channel_config_metadata(agent_id, CHANNEL_TYPE)
    return channel_config_out(config)


@router.get("/agents/{agent_id}/google-chat-channel/webhook-url", response_model=None)
async def get_google_chat_webhook_url(
    agent_id: uuid.UUID,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
) -> WebhookUrlResponse:
    _ = await check_agent_access(current_user, agent_id)
    public_base = await platform_service.get_public_base_url(request)
    return {"webhook_url": f"{public_base}/api/channel/google-chat/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/google-chat-channel", status_code=204)
async def delete_google_chat_channel(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> Response:
    await channel_config.require_channel_creator(current_user, agent_id)
    await channel_config.delete_channel_config(agent_id, CHANNEL_TYPE)
    return Response(status_code=204)


@router.post("/channel/google-chat/{agent_id}/webhook", response_model=None)
async def google_chat_event_webhook(agent_id: uuid.UUID, request: Request) -> Response | dict[str, object]:
    """HTTP endpoint for a Google Chat app (interaction events).

    Configure the Chat app's HTTP endpoint URL to the webhook-url from
    ``GET /agents/{id}/google-chat-channel/webhook-url``.

    Every message is accepted natively before the webhook answers ``{}``: its receipt, transcript and gateway row
    commit together, OpenClaw runs the turn later and the report relay posts the reply through the Chat API with the
    service account. A message without ``message.name`` has no identity to dedupe on, so it is refused with 400 before
    any download, sender resolution or write. A message without a sender, or from a sender who is not a linked member,
    is acknowledged and ignored.
    """
    body_bytes = await read_bounded_body(request, CHANNEL_WEBHOOK_MAX_BODY_SIZE)
    if body_bytes is None:
        return Response(status_code=413)

    config = await channel_config.get_channel_config(agent_id, CHANNEL_TYPE)
    if not config or not config.is_configured:
        return Response(status_code=404)

    try:
        body: JsonValue = _decode_event_body(body_bytes, json.loads) if body_bytes else {}
    except ValueError, UnicodeError:
        return Response(status_code=400)
    if not isinstance(body, dict):
        return Response(status_code=400)

    audience = gchat.audience_for_config(config)
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
    if audience:
        try:
            _ = await gchat.verify_google_chat_bearer(auth_header, audience)
        except ValueError as exc:
            logger.warning("[GoogleChat] Auth failed for agent %s: %s", agent_id, exc)
            return Response(status_code=401)
    elif config.verification_token not in (None, ""):
        token = str(body.get("token") or "").strip()
        expected = config.verification_token
        if not hmac.compare_digest(token.encode(errors="surrogatepass"), expected.encode(errors="surrogatepass")):
            logger.warning("[GoogleChat] verification_token mismatch for agent %s", agent_id)
            return Response(status_code=401)
    else:
        logger.warning(
            "[GoogleChat] No audience/project_number or verification_token configured for agent %s",
            agent_id,
        )
        return Response(status_code=401)

    event = gchat.parse_google_chat_event(body)
    if event is None:
        return {}

    if event.event_type == "REMOVED_FROM_SPACE":
        return {}

    if event.event_type == "ADDED_TO_SPACE" and not event.text and not event.has_attachment:
        return gchat.sync_text_response("MaraClaw is ready. Send a message to get started.")

    # A message, or an ADDED_TO_SPACE event that carries one, always has text or an attachment here.
    if event.event_type not in {"MESSAGE", "ADDED_TO_SPACE"}:
        return {}

    if event.message_name is None:
        raise HTTPException(status_code=400, detail="Google Chat message name is required")
    if not (event.sender_name or event.sender_email):
        return {}

    try:
        await _accept_native_google_event(agent_id, config, event)
    except ChannelUserResolutionError:
        logger.info("[GoogleChat] Ignored a message from a sender who is not a linked member of agent %s", agent_id)
    return {}


async def _accept_native_google_event(
    agent_id: uuid.UUID, config: ChannelConfigRecord, event: gchat.GoogleChatInbound
) -> None:
    from app.services.channels.google_chat_media import GoogleChatAttachment, accept_google_chat_media
    from app.services.channels.submissions import ChannelEvent, accept_channel_event

    message = json_object_from(event.raw.get("message"))
    attachments_raw = message.get("attachment") or message.get("attachments")
    attachments = attachments_raw if isinstance(attachments_raw, list) else []
    native = ChannelEvent(
        config_id=config.id,
        agent_id=agent_id,
        provider=CHANNEL_TYPE,
        event_key=json.dumps([config.app_id, event.space_name, event.message_name], separators=(",", ":")),
        sender_id=event.sender_name or event.sender_email,
        conversation_id=gchat.external_conv_id_for_inbound(event),
        text=event.text,
        kind="attachments" if event.has_attachment else "text",
        media_ids=tuple(json_as_str_or(json_object_from(item).get("name")) for item in attachments),
        is_group=gchat.is_group_space(event),
    )
    if attachments:
        await accept_google_chat_media(
            native, config, tuple(GoogleChatAttachment.model_validate(item) for item in attachments)
        )
    else:
        await accept_channel_event(native)
