"""Google Chat app channel API routes."""

from __future__ import annotations

import asyncio
import hmac
import json
import uuid
from typing import Any, TypedDict

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.core.json_types import json_loads_value, json_object_from
from app.core.logging import logger
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.records.channel_config import ChannelConfigRecord
from app.records.user import UserRecord
from app.schemas.schemas import ChannelConfigOut
from app.services.channels import (
    config as channel_config,
    dedup as channel_dedup,
    google_chat as gchat,
    inbound as channel_inbound,
)
from app.services.channels.redact import channel_config_out
from app.services.platform_service import platform_service

router = APIRouter(tags=["google_chat"])

CHANNEL_TYPE = "google_chat"
_DEDUP_NS = "google_chat"
# Keep strong refs so background tasks are not GC'd mid-flight (RUF006).
_background_tasks: set[asyncio.Task[None]] = set()


class GoogleChatChannelPayload(TypedDict, total=False):
    """Config payload for a Google Chat app bound to an agent.

    project_number: GCP project number used as JWT audience (required for verify).
    service_account_json: optional SA JSON for proactive / async Chat API sends.
    client_email: optional display / metadata only (do not put PEM in encrypt_key).
    audience: optional JWT audience override.
    verification_token: optional legacy shared token compared to body.token.
    """

    project_number: str
    service_account_json: str | dict[str, Any]
    client_email: str
    audience: str
    verification_token: str


@router.post("/agents/{agent_id}/google-chat-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_google_chat_channel(
    agent_id: uuid.UUID,
    data: GoogleChatChannelPayload,
    current_user: UserRecord = Depends(get_current_user),
):
    """Configure Google Chat for an agent."""
    await channel_config.require_channel_creator(current_user, agent_id)

    project_number = str(data.get("project_number") or "").strip()
    if not project_number:
        raise HTTPException(status_code=422, detail="project_number is required")

    client_email = str(data.get("client_email") or "").strip()
    audience = str(data.get("audience") or project_number).strip()
    verification_token = str(data.get("verification_token") or "").strip() or None

    extra: dict[str, Any] = {"audience": audience}
    sa_json = data.get("service_account_json")
    if isinstance(sa_json, dict):
        extra["service_account_json"] = sa_json
        client_email = client_email or str(sa_json.get("client_email") or "").strip()
    elif isinstance(sa_json, str) and sa_json.strip():
        try:
            parsed_raw = json_loads_value(sa_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail="service_account_json must be valid JSON") from exc
        if not isinstance(parsed_raw, dict):
            raise HTTPException(status_code=422, detail="service_account_json must be a JSON object")
        parsed = json_object_from(parsed_raw)
        if "private_key" not in parsed or "client_email" not in parsed:
            raise HTTPException(
                status_code=422,
                detail="service_account_json must include client_email and private_key",
            )
        extra["service_account_json"] = parsed
        client_email = client_email or str(parsed.get("client_email") or "").strip()

    # Never store SA PEM in encrypt_key (VARCHAR(255) is too small).
    # Keep encrypt_key cleared for this channel type.
    config = await channel_config.upsert_channel_config(
        agent_id=agent_id,
        channel_type=CHANNEL_TYPE,
        app_id=project_number,
        app_secret=client_email or None,
        encrypt_key=None,
        verification_token=verification_token,
        extra_config=extra,
        is_configured=True,
    )
    return channel_config_out(config)


@router.get("/agents/{agent_id}/google-chat-channel", response_model=ChannelConfigOut)
async def get_google_chat_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    agent, _ = await check_agent_access(current_user, agent_id)
    # Secrets: only creator/manage path should see config metadata; still redacted.
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can view channel credentials")
    config = await channel_config.require_channel_config(agent_id, CHANNEL_TYPE)
    return channel_config_out(config)


@router.get("/agents/{agent_id}/google-chat-channel/webhook-url")
async def get_google_chat_webhook_url(
    agent_id: uuid.UUID,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: object | None = None,
):
    _ = await check_agent_access(current_user, agent_id)
    public_base = await platform_service.get_public_base_url(db, request)
    return {"webhook_url": f"{public_base}/api/channel/google-chat/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/google-chat-channel", status_code=204)
async def delete_google_chat_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    await channel_config.require_channel_creator(current_user, agent_id)
    await channel_config.delete_channel_config(agent_id, CHANNEL_TYPE)
    return Response(status_code=204)


async def _process_message_event(
    *,
    agent_id: uuid.UUID,
    config: ChannelConfigRecord,
    event: gchat.GoogleChatInbound,
) -> None:
    """Run LLM and deliver reply (async path after webhook ack)."""
    agent = await channel_inbound.load_agent(agent_id)
    if not agent:
        logger.warning("[GoogleChat] Agent %s not found", agent_id)
        return

    external_user_id = event.sender_name or event.sender_email or "unknown"
    platform_user = await channel_inbound.resolve_sender_user(
        agent=agent,
        channel_type=CHANNEL_TYPE,
        external_user_id=external_user_id,
        extra_info={
            "name": event.sender_display_name or f"Google Chat User {external_user_id[-8:]}",
            "email": event.sender_email,
            "external_id": external_user_id,
        },
    )

    is_group = gchat.is_group_space(event)
    conv_id = gchat.external_conv_id_for_inbound(event)
    session_user_id = agent.creator_id if is_group and agent.creator_id else platform_user.id
    group_label = event.space_display_name or event.space_name
    session = await channel_inbound.open_channel_session(
        agent_id=agent_id,
        user_id=session_user_id,
        external_conv_id=conv_id,
        source_channel=CHANNEL_TYPE,
        first_message_title=event.text or "Google Chat",
        is_group=is_group,
        group_name=group_label if is_group else None,
    )

    user_text = event.text
    if event.has_attachment and not user_text:
        # Attachments not downloaded yet - honest failure instead of empty LLM turn.
        notice = (
            "I received an attachment, but Google Chat attachment download is not enabled yet. "
            + "Please send the content as text, or re-send with a text caption."
        )
        await channel_inbound.persist_assistant_message(
            agent_id=agent_id,
            user_id=platform_user.id,
            session=session,
            content=notice,
        )
        if gchat.has_service_account(config) and event.space_name:
            try:
                _ = await gchat.send_google_chat_message(
                    config,
                    space_name=event.space_name,
                    text=notice,
                    thread_name=event.thread_name,
                )
            except Exception:
                logger.exception("[GoogleChat] Failed to send attachment notice")
        return

    if not user_text:
        return

    history = await channel_inbound.load_history_for_session(
        agent_id=agent_id,
        session=session,
        context_window_size=agent.context_window_size,
    )
    await channel_inbound.persist_user_message(
        agent_id=agent_id,
        user_id=platform_user.id,
        session=session,
        content=user_text,
    )

    try:
        reply_text = await channel_inbound.generate_channel_reply(
            agent_id=agent_id,
            user_text=user_text,
            history=history,
            user_id=platform_user.id,
            session_id=str(session.id),
        )
    except Exception:
        logger.exception("[GoogleChat] LLM failed for agent %s", agent_id)
        reply_text = "Sorry - I hit an error generating a reply. Please try again."

    await channel_inbound.persist_assistant_message(
        agent_id=agent_id,
        user_id=platform_user.id,
        session=session,
        content=reply_text,
    )
    logger.info("[GoogleChat] Reply to agent %s: %s", agent_id, reply_text[:80])

    if gchat.has_service_account(config) and event.space_name:
        try:
            _ = await gchat.send_google_chat_message(
                config,
                space_name=event.space_name,
                text=reply_text,
                thread_name=event.thread_name,
            )
        except Exception:
            logger.exception("[GoogleChat] Failed async delivery for agent %s", agent_id)


@router.post("/channel/google-chat/{agent_id}/webhook", response_model=None)
async def google_chat_event_webhook(agent_id: uuid.UUID, request: Request) -> Response | dict[str, Any]:
    """HTTP endpoint for a Google Chat app (interaction events).

    Configure the Chat app's HTTP endpoint URL to the webhook-url from
    ``GET /agents/{id}/google-chat-channel/webhook-url``.

    When a service account is configured, the webhook acknowledges quickly and
    delivers the LLM reply asynchronously via the Chat API (within Google's ~30s
    sync budget for the ack). Without a service account, a short sync reply is
    returned only for membership events / errors; messages still attempt a bounded
    sync LLM path as a fallback.
    """
    config = await channel_config.get_channel_config(agent_id, CHANNEL_TYPE)
    if not config or not config.is_configured:
        return Response(status_code=404)

    body_bytes = await request.body()
    try:
        body: dict[str, Any] = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError:
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
    elif config.verification_token:
        token = str(body.get("token") or "").strip()
        expected = config.verification_token
        if not hmac.compare_digest(token, expected):
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

    # Membership: welcome, and process embedded first message if present.
    if event.event_type == "REMOVED_FROM_SPACE":
        return {}

    if event.event_type == "ADDED_TO_SPACE" and not event.text and not event.has_attachment:
        return gchat.sync_text_response("MaraClaw is ready. Send a message to get started.")

    if event.event_type not in {"MESSAGE", "ADDED_TO_SPACE"}:
        return {}

    if event.event_type == "ADDED_TO_SPACE" and (event.text or event.has_attachment):
        # Treat embedded message as a MESSAGE for processing.
        pass
    elif event.event_type != "MESSAGE":
        return {}

    if event.message_name:
        dedupe_key = event.message_name
    else:
        import hashlib

        raw = f"{event.space_name}:{event.sender_name}:{event.text[:64]}"
        dedupe_key = hashlib.sha256(raw.encode()).hexdigest()
    if await channel_dedup.already_processed_shared(_DEDUP_NS, dedupe_key):
        return {}

    use_async = gchat.has_service_account(config) and bool(event.space_name)

    if use_async:
        # Ack immediately so Google does not time out; process in background.
        async def _bg() -> None:
            try:
                await _process_message_event(agent_id=agent_id, config=config, event=event)
                await channel_dedup.mark_processed_shared(_DEDUP_NS, dedupe_key)
            except Exception:
                logger.exception("[GoogleChat] Background processing failed for agent %s", agent_id)

        task = asyncio.create_task(_bg())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        # Empty body is a valid Chat app ack for async handling.
        return {}

    # Sync fallback (no SA): run inline and return text body.
    try:
        agent = await channel_inbound.load_agent(agent_id)
        if not agent:
            return {}
        external_user_id = event.sender_name or event.sender_email or "unknown"
        platform_user = await channel_inbound.resolve_sender_user(
            agent=agent,
            channel_type=CHANNEL_TYPE,
            external_user_id=external_user_id,
            extra_info={
                "name": event.sender_display_name or f"Google Chat User {external_user_id[-8:]}",
                "email": event.sender_email,
                "external_id": external_user_id,
            },
        )
        is_group = gchat.is_group_space(event)
        conv_id = gchat.external_conv_id_for_inbound(event)
        session_user_id = agent.creator_id if is_group and agent.creator_id else platform_user.id
        session = await channel_inbound.open_channel_session(
            agent_id=agent_id,
            user_id=session_user_id,
            external_conv_id=conv_id,
            source_channel=CHANNEL_TYPE,
            first_message_title=event.text or "Google Chat",
            is_group=is_group,
            group_name=(event.space_display_name or event.space_name) if is_group else None,
        )
        if event.has_attachment and not event.text:
            msg = (
                "I received an attachment, but attachment handling requires a service account "
                + "for async delivery. Please send text, or configure service_account_json."
            )
            await channel_dedup.mark_processed_shared(_DEDUP_NS, dedupe_key)
            return gchat.sync_text_response(msg, thread_name=event.thread_name)
        if not event.text:
            await channel_dedup.mark_processed_shared(_DEDUP_NS, dedupe_key)
            return {}

        history = await channel_inbound.load_history_for_session(
            agent_id=agent_id,
            session=session,
            context_window_size=agent.context_window_size,
        )
        await channel_inbound.persist_user_message(
            agent_id=agent_id,
            user_id=platform_user.id,
            session=session,
            content=event.text,
        )
        try:
            reply_text = await asyncio.wait_for(
                channel_inbound.generate_channel_reply(
                    agent_id=agent_id,
                    user_text=event.text,
                    history=history,
                    user_id=platform_user.id,
                    session_id=str(session.id),
                ),
                timeout=25.0,
            )
        except TimeoutError:
            reply_text = (
                "I'm still working on that, but Google Chat timed out waiting for a sync reply. "
                + "Configure a service account for reliable async replies."
            )
        except Exception:
            logger.exception("[GoogleChat] Sync LLM failed for agent %s", agent_id)
            reply_text = "Sorry - I hit an error generating a reply. Please try again."

        await channel_inbound.persist_assistant_message(
            agent_id=agent_id,
            user_id=platform_user.id,
            session=session,
            content=reply_text,
        )
        await channel_dedup.mark_processed_shared(_DEDUP_NS, dedupe_key)
        return gchat.sync_text_response(reply_text, thread_name=event.thread_name)
    except Exception:
        logger.exception("[GoogleChat] Sync path failed for agent %s", agent_id)
        return gchat.sync_text_response(
            "Sorry - something went wrong handling your message.",
            thread_name=event.thread_name,
        )
