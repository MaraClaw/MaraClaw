"""Slack Bot Channel API routes."""

import hashlib
import hmac
import time
import uuid
from collections.abc import Mapping
from typing import TypedDict

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.core.domain.channels import ChannelType
from app.core.json_types import json_as_str_or, json_loads_object, json_object_from, json_object_from_response
from app.core.logging import logger
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.request_body import CHANNEL_WEBHOOK_MAX_BODY_SIZE, read_bounded_body
from app.core.security import get_current_user
from app.dao.channel_config_dao import channel_config_dao
from app.records.user import UserRecord
from app.schemas.channel_response_contracts import SlackChallengeResponse, WebhookUrlResponse
from app.schemas.content_response_catalog import OkAcknowledgement
from app.schemas.schemas import ChannelConfigOut
from app.services.channel_user_service import ChannelUserResolutionError

router = APIRouter(tags=["slack"])

SLACK_MSG_LIMIT = 4000  # Slack text message char limit
# Slack redelivers a failed or slow event up to three times (X-Slack-Retry-Num 1 to 3); the third is the last.
SLACK_FINAL_RETRY_NUM = "3"


class SlackChannelPayload(TypedDict, total=False):
    bot_token: str
    signing_secret: str


# ─── Config CRUD ────────────────────────────────────────


@router.post("/agents/{agent_id}/slack-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_slack_channel(
    agent_id: uuid.UUID, data: SlackChannelPayload, current_user: UserRecord = Depends(get_current_user)
) -> ChannelConfigOut:
    """Configure Slack bot for an agent. Fields: bot_token, signing_secret."""
    from app.services.channels import config as channel_cfg
    from app.services.channels.redact import channel_config_out

    await channel_cfg.require_channel_creator(current_user, agent_id)

    bot_token = data.get("bot_token", "").strip()
    signing_secret = data.get("signing_secret", "").strip()
    if not bot_token or not signing_secret:
        raise HTTPException(status_code=422, detail="bot_token and signing_secret are required")

    config = await channel_cfg.upsert_channel_config(
        agent_id=agent_id,
        channel_type="slack",
        app_id="slack",
        app_secret=bot_token,
        encrypt_key=signing_secret,
        is_configured=True,
    )
    return channel_config_out(config)


@router.get("/agents/{agent_id}/slack-channel", response_model=ChannelConfigOut)
async def get_slack_channel(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> ChannelConfigOut:
    from app.services.channels import config as channel_cfg
    from app.services.channels.redact import channel_config_out

    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can view channel credentials")
    config = await channel_cfg.require_channel_config_metadata(agent_id, "slack")
    return channel_config_out(config)


@router.get("/agents/{agent_id}/slack-channel/webhook-url", response_model=None)
async def get_slack_webhook_url(
    agent_id: uuid.UUID,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
) -> WebhookUrlResponse:
    from app.services.platform_service import platform_service

    _ = await check_agent_access(current_user, agent_id)
    public_base = await platform_service.get_public_base_url(request)
    return {"webhook_url": f"{public_base}/api/channel/slack/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/slack-channel", status_code=204, response_model=None)
async def delete_slack_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)) -> None:
    from app.services.channels import config as channel_cfg

    await channel_cfg.require_channel_creator(current_user, agent_id)
    await channel_cfg.delete_channel_config(agent_id, "slack")


# ─── Event Webhook ──────────────────────────────────────


def _verify_slack_signature(signing_secret: str, body: bytes, headers: Mapping[str, str]) -> bool:
    """Verify Slack's HMAC-SHA256 request signature."""
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    if not ts or not sig:
        return False
    try:
        timestamp = int(ts)
        base = f"v0:{ts}:{body.decode('utf-8')}".encode()
    except ValueError, UnicodeError:
        return False
    if abs(int(time.time()) - timestamp) > 300:
        return False
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), sig.encode())


async def _send_slack_messages(bot_token: str, channel: str, text: str, *, strict: bool = False) -> None:
    """Send text to Slack, splitting into SLACK_MSG_LIMIT chunks if needed."""
    import httpx

    chunks = [text[i : i + SLACK_MSG_LIMIT] for i in range(0, len(text), SLACK_MSG_LIMIT)]
    async with httpx.AsyncClient(timeout=10) as client:
        for chunk in chunks:
            resp = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
                json={"channel": channel, "text": chunk},
            )
            if strict:
                _ = resp.raise_for_status()
                data = json_object_from_response(resp)
                if data.get("ok") is not True:
                    raise RuntimeError("Slack rejected the message")


@router.post("/channel/slack/{agent_id}/webhook", response_model=None)
async def slack_event_webhook(
    agent_id: uuid.UUID, request: Request
) -> Response | OkAcknowledgement | SlackChallengeResponse:
    """Handle Slack Event API callbacks."""
    body_bytes = await read_bounded_body(request, CHANNEL_WEBHOOK_MAX_BODY_SIZE)
    if body_bytes is None:
        return Response(status_code=413)

    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.SLACK)
    if not config:
        return Response(status_code=404)

    signing_secret = config.encrypt_key or ""
    if not config.is_configured:
        return Response(status_code=404)
    if not signing_secret:
        logger.warning("[Slack] Missing signing secret for agent %s", agent_id)
        return Response(status_code=401)
    if not _verify_slack_signature(signing_secret, body_bytes, dict(request.headers)):
        return Response(status_code=401)

    try:
        body = json_loads_object(body_bytes)
    except ValueError, UnicodeError:
        return Response(status_code=401)
    logger.info(f"[Slack] Webhook for {agent_id}: type={body.get('type')}")

    if body.get("type") == "url_verification":
        return {"challenge": body["challenge"]}

    if body.get("type") != "event_callback":
        return {"ok": True}

    event = json_object_from(body.get("event"))

    if ((bot_id := event.get("bot_id")) is not None and bot_id) or (
        (subtype := event.get("subtype")) is not None and subtype and subtype != "file_share"
    ):
        return {"ok": True}

    event_type = event.get("type", "")
    if event_type not in ("message", "app_mention"):
        return {"ok": True}

    user_text = json_as_str_or(event.get("text")).strip()
    import re

    user_text = re.sub(r"^<@[A-Z0-9]+>\s*", "", user_text).strip()

    files_raw = event.get("files")
    slack_files = [json_object_from(item) for item in files_raw] if isinstance(files_raw, list) else []

    if not user_text and not slack_files:
        return {"ok": True}

    from app.services.channels.slack_submissions import accept_slack_event

    final_retry = request.headers.get("x-slack-retry-num") == SLACK_FINAL_RETRY_NUM
    try:
        await accept_slack_event(config, body, final_retry=final_retry)
    except ChannelUserResolutionError as exc:
        logger.info("[Slack] Rejected channel sender for agent %s: %s", agent_id, exc)
    return {"ok": True}
