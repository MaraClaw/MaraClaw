"""Microsoft Teams Bot Channel API routes."""

import hashlib
import importlib
import json
import os
import time
import uuid
from datetime import UTC, datetime
from functools import partial
from typing import Protocol, TypedDict, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.config import get_settings
from app.core.domain.channels import ChannelType
from app.core.json_types import (
    JsonObject,
    is_json_object,
    json_as_str,
    json_as_str_or,
    json_object_from,
    json_object_from_response,
)
from app.core.logging import logger
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.request_body import CHANNEL_WEBHOOK_MAX_BODY_SIZE, read_bounded_body
from app.core.security import get_current_user
from app.dao.channel_config_dao import channel_config_dao
from app.records.channel_config import ChannelConfigRecord
from app.records.user import UserRecord
from app.schemas.channel_response_contracts import WebhookUrlResponse
from app.schemas.content_response_catalog import OkAcknowledgement
from app.schemas.schemas import ChannelConfigOut
from app.services import im_token_cache
from app.services.channels.config import get_channel_config_metadata
from app.services.channels.redact import channel_config_out
from app.services.channels.teams_auth import (
    TeamsAuthenticationError,
    authenticate_teams_activity,
    is_bot_framework_service_url,
)
from app.services.channels.teams_media import accept_teams_media, parse_teams_media
from app.services.storage_runtime.quota_errors import StorageQuotaError

settings = get_settings()

router = APIRouter(tags=["microsoft_teams"])

TEAMS_MSG_LIMIT = 28000  # Teams message char limit (approx 28KB)
DEFAULT_CONTEXT_WINDOW_SIZE = 100


class TeamsChannelPayload(TypedDict, total=False):
    app_id: str
    app_secret: str
    tenant_id: str
    use_managed_identity: bool


_BOT_FRAMEWORK_SCOPE = "https://api.botframework.com/.default"


class _AzureToken(Protocol):
    token: object
    expires_on: object


class _AzureCredential(Protocol):
    async def get_token(self, scope: str) -> _AzureToken: ...

    async def close(self) -> object: ...


class _AzureIdentityAio(Protocol):
    DefaultAzureCredential: type[_AzureCredential]


def _extra_config(config: ChannelConfigRecord) -> JsonObject:
    return json_object_from(config.extra_config)


def _json_mapping(value: object) -> JsonObject:
    return json_object_from(value)


_KNOWN_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
        "access_denied",
        "interaction_required",
        "temporarily_unavailable",
        "server_error",
        "BadArgument",
        "MissingProperty",
        "MessageSizeTooBig",
        "ServiceError",
        "BotNotInConversationRoster",
        "ConversationBlockedByUser",
        "ConversationNotFound",
    }
)


def _provider_error_code(response: httpx.Response) -> str:
    """Classify a failed provider reply for logs: an allowlisted code, ``other``, ``none`` or ``unparsed``.

    Descriptions, messages and bodies are remote free text and can echo credentials, so they are never read.
    """
    try:
        data = json_object_from_response(response)
    except ValueError:
        return "unparsed"
    error = data.get("error")
    code = json_as_str(error.get("code") if is_json_object(error) else error)
    if code is None:
        return "none"
    return code if code in _KNOWN_ERROR_CODES else "other"


def _uses_managed_identity(config: ChannelConfigRecord) -> bool:
    return bool(_extra_config(config).get("use_managed_identity", False))


def _tenant_id(config: ChannelConfigRecord) -> str:
    tenant_raw = _extra_config(config).get("tenant_id")
    if isinstance(tenant_raw, str) and tenant_raw:
        return tenant_raw
    return os.environ.get("TEAMS_TENANT_ID") or "common"


def _teams_token_key(config: ChannelConfigRecord) -> im_token_cache.ImTokenKey:
    """One cache entry per bot app and tenant; a client secret is held only as a digest, a managed identity has none."""
    app_id, tenant_id = config.app_id or "", _tenant_id(config)
    if _uses_managed_identity(config):
        return im_token_cache.ImTokenKey("teams_managed_identity", app_id, tenant_id)
    secret_digest = hashlib.sha256((config.app_secret or "").encode()).hexdigest()
    return im_token_cache.ImTokenKey("teams", app_id, f"{tenant_id}\0{secret_digest}")


async def _drop_rejected_teams_token(config: ChannelConfigRecord, rejected: str) -> None:
    """Drop the cached token Teams rejected, unless a fresher one has replaced it."""
    await im_token_cache.drop_rejected_im_token(_teams_token_key(config), rejected)


async def _get_teams_access_token(config: ChannelConfigRecord) -> str | None:
    """Get the shared cached Microsoft Teams access token, minting one on a miss.

    Supports:
    - Client credentials (app_id + app_secret) - default
    - Managed Identity (when use_managed_identity is True in extra_config)
    """
    if _uses_managed_identity(config):
        return await _managed_identity_token(config)
    return await _client_credentials_token(config)


async def _managed_identity_token(config: ChannelConfigRecord) -> str | None:
    agent_id = str(config.agent_id)

    async def mint() -> im_token_cache.MintedToken:
        return await _mint_managed_identity_token(agent_id)

    try:
        token = await im_token_cache.get_or_mint_im_token(_teams_token_key(config), mint)
    except ImportError:
        logger.error("Teams: azure-identity package not installed. Install it with: pip install azure-identity")
        return None
    except Exception as e:
        logger.error(
            "Teams: managed identity token request failed agent={} kind=identity error_type={}",
            agent_id,
            type(e).__name__,
        )
        return None
    if not token:
        logger.error("Teams: managed identity token request failed agent={} kind=malformed_response", agent_id)
        return None
    return token


async def _mint_managed_identity_token(agent_id: str) -> im_token_cache.MintedToken:
    azure_identity = cast("_AzureIdentityAio", importlib.import_module("azure.identity.aio"))
    credential = azure_identity.DefaultAzureCredential()
    try:
        token = await credential.get_token(_BOT_FRAMEWORK_SCOPE)
        access_token_raw: object = token.token
        expires_on_raw: object = token.expires_on
    finally:
        _ = await credential.close()
    if not isinstance(access_token_raw, str) or not isinstance(expires_on_raw, (int, float)):
        return im_token_cache.MintedToken("")
    logger.info(
        "Teams: Successfully obtained access token via managed identity for agent {}, expires at {}",
        agent_id,
        expires_on_raw,
    )
    return im_token_cache.MintedToken(access_token_raw, max(int(expires_on_raw - time.time()), 1))


async def _client_credentials_token(config: ChannelConfigRecord) -> str | None:
    agent_id = str(config.agent_id)
    app_id = config.app_id
    app_secret = config.app_secret
    if app_id in (None, "") or app_secret in (None, ""):
        logger.error(f"Teams: Missing app_id or app_secret for agent {agent_id}")
        return None

    token_url = f"https://login.microsoftonline.com/{_tenant_id(config)}/oauth2/v2.0/token"
    data = {
        "client_id": app_id,
        "client_secret": app_secret,
        "grant_type": "client_credentials",
        "scope": _BOT_FRAMEWORK_SCOPE,
    }

    async def mint() -> im_token_cache.MintedToken:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(token_url, data=data)
        if resp.status_code != 200:
            raise im_token_cache.ImTokenRefusedError(resp)
        try:
            token_data = json_object_from_response(resp)
        except ValueError:
            token_data = {}
        access_token = token_data.get("access_token")
        expires_in = token_data.get("expires_in")
        if not isinstance(access_token, str) or not isinstance(expires_in, (int, float)):
            return im_token_cache.MintedToken("")
        logger.info("Teams: Successfully obtained access token for agent {}, expires in {}s", agent_id, expires_in)
        return im_token_cache.MintedToken(access_token, max(int(expires_in), 1))

    try:
        token = await im_token_cache.get_or_mint_im_token(_teams_token_key(config), mint)
    except im_token_cache.ImTokenRefusedError as refused:
        logger.error(
            "Teams: OAuth token request failed agent={} kind=http_status status={} error_code={}",
            agent_id,
            refused.response.status_code,
            _provider_error_code(refused.response),
        )
        return None
    except Exception as e:
        kind = "transport" if isinstance(e, httpx.HTTPError) else "unexpected"
        logger.error(
            "Teams: OAuth token request failed agent={} kind={} error_type={}", agent_id, kind, type(e).__name__
        )
        return None
    if not token:
        logger.error("Teams: OAuth token request failed agent={} kind=malformed_response", agent_id)
        return None
    return token


async def _send_teams_message(
    config: ChannelConfigRecord, conversation_id: str, activity: JsonObject, *, strict: bool = False
) -> None:
    """Send an activity (message) to Microsoft Teams."""
    service_url = _extra_config(config).get("service_url")
    if not isinstance(service_url, str) or not is_bot_framework_service_url(service_url):
        raise ValueError("Invalid Bot Framework service URL")

    access_token = await _get_teams_access_token(config)
    if access_token in (None, ""):
        logger.error(f"Teams: No access token for agent {config.agent_id}, cannot send message")
        raise ValueError("No access token available")

    if "type" not in activity:
        activity["type"] = "message"
    if "timestamp" not in activity:
        activity["timestamp"] = datetime.now(UTC).isoformat() + "Z"

    if (reply_to_id := activity.get("replyToId")) is not None and reply_to_id and "id" not in activity:
        activity["id"] = str(uuid.uuid4())

    text_value = activity.get("text", "")
    text_content = text_value if isinstance(text_value, str) else ""
    if len(text_content.encode("utf-8")) > TEAMS_MSG_LIMIT:
        chunks = [text_content[i : i + TEAMS_MSG_LIMIT] for i in range(0, len(text_content), TEAMS_MSG_LIMIT)]
        for i, chunk in enumerate(chunks):
            chunk_activity = {**activity, "text": chunk}
            if i > 0:
                chunk_activity.pop("replyToId", None)
            access_token = await _send_activity_renewing_token(
                config, access_token, service_url, conversation_id, chunk_activity, strict=strict
            )
    else:
        _ = await _send_activity_renewing_token(
            config, access_token, service_url, conversation_id, activity, strict=strict
        )


async def _send_activity_renewing_token(
    config: ChannelConfigRecord,
    access_token: str,
    service_url: str,
    conversation_id: str,
    activity: JsonObject,
    *,
    strict: bool,
) -> str:
    """Send one activity; a 401 drops the rejected token and retries once with a fresh one.

    Returns the token that delivered the activity, so later chunks reuse it.
    """
    try:
        await _send_teams_message_single_chunk(access_token, service_url, conversation_id, activity, strict=strict)
    except httpx.HTTPStatusError as rejected:
        if rejected.response.status_code != 401:
            raise
        await _drop_rejected_teams_token(config, access_token)
        renewed = await _get_teams_access_token(config)
        if renewed in (None, ""):
            raise ValueError("No access token available") from rejected
        access_token = renewed
        await _send_teams_message_single_chunk(access_token, service_url, conversation_id, activity, strict=strict)
    return access_token


async def _send_teams_message_single_chunk(
    access_token: str, service_url: str, conversation_id: str, activity: JsonObject, *, strict: bool = False
) -> None:
    """Send a single chunked message to Microsoft Teams."""
    service_url_clean = service_url.rstrip("/")
    post_url = f"{service_url_clean}/v3/conversations/{conversation_id}/activities"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(post_url, headers=headers, json=activity)
    except httpx.TransportError as e:
        logger.error(
            "Teams: send failed kind=transport error_type={} conversation={}", type(e).__name__, conversation_id
        )
        raise
    if resp.status_code != 200:
        logger.error(
            "Teams: send failed kind=http_status status={} error_code={} conversation={}",
            resp.status_code,
            _provider_error_code(resp),
            conversation_id,
        )
    _ = resp.raise_for_status()
    if strict and not _send_acknowledged(resp, conversation_id):
        raise RuntimeError("Teams did not acknowledge the message")
    logger.info(f"Teams: Sent message to conversation {conversation_id}")


def _send_acknowledged(resp: httpx.Response, conversation_id: str) -> bool:
    """Whether the provider returned an activity id; an unreadable reply logs a bounded event and re-raises."""
    try:
        acknowledged = bool(json_as_str(json_object_from_response(resp).get("id")))
    except ValueError:
        logger.error("Teams: send failed kind=malformed_response conversation={}", conversation_id)
        raise
    if not acknowledged:
        logger.error("Teams: send failed kind=malformed_response conversation={}", conversation_id)
    return acknowledged


# ─── Config CRUD ────────────────────────────────────────


@router.post("/agents/{agent_id}/teams-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_teams_channel(
    agent_id: uuid.UUID, data: TeamsChannelPayload, current_user: UserRecord = Depends(get_current_user)
) -> ChannelConfigOut:
    """Configure Microsoft Teams bot for an agent. Fields: app_id, app_secret."""
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    app_id = data.get("app_id", "").strip()
    app_secret = data.get("app_secret", "").strip()
    tenant_id = data.get("tenant_id", "").strip()
    use_managed_identity = data.get("use_managed_identity", False)

    if not app_id or (not use_managed_identity and not app_secret):
        raise HTTPException(
            status_code=422, detail="app_id is required; app_secret is required without managed identity"
        )

    existing = await get_channel_config_metadata(agent_id, ChannelType.MICROSOFT_TEAMS)
    if existing:
        extra = json_object_from(existing.extra_config)
        if tenant_id:
            extra["tenant_id"] = tenant_id
        elif "tenant_id" in extra and not tenant_id:
            extra.pop("tenant_id", None)
        extra["use_managed_identity"] = use_managed_identity
        obj_in: dict[str, object] = {
            "app_id": app_id,
            "is_configured": True,
            "extra_config": extra,
        }
        if not use_managed_identity:
            obj_in["app_id"] = app_id
            obj_in["app_secret"] = app_secret
        config = await channel_config_dao.update(db_obj=existing, obj_in=obj_in)
        return channel_config_out(config or existing)

    extra_config: JsonObject = {}
    if tenant_id:
        extra_config["tenant_id"] = tenant_id
    if use_managed_identity:
        extra_config["use_managed_identity"] = True

    config = await channel_config_dao.create(
        obj_in={
            "agent_id": agent_id,
            "channel_type": "microsoft_teams",
            "app_id": app_id,
            "app_secret": app_secret if not use_managed_identity else None,
            "is_configured": True,
            "extra_config": extra_config,
        }
    )
    return channel_config_out(config)


@router.get("/agents/{agent_id}/teams-channel", response_model=ChannelConfigOut)
async def get_teams_channel(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> ChannelConfigOut:
    """Get Microsoft Teams channel configuration for an agent."""
    _ = await check_agent_access(current_user, agent_id)
    config = await get_channel_config_metadata(agent_id, ChannelType.MICROSOFT_TEAMS)
    if not config:
        raise HTTPException(status_code=404, detail="Microsoft Teams not configured")
    return channel_config_out(config)


@router.get("/agents/{agent_id}/teams-channel/webhook-url", response_model=None)
async def get_teams_webhook_url(
    agent_id: uuid.UUID,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
) -> WebhookUrlResponse:
    """Get the Microsoft Teams webhook URL for an agent."""
    _ = await check_agent_access(current_user, agent_id)
    from app.services.platform_service import platform_service

    public_base = await platform_service.get_public_base_url(request)
    return {"webhook_url": f"{public_base}/api/channel/teams/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/teams-channel", status_code=204, response_model=None)
async def delete_teams_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)) -> None:
    """Delete Microsoft Teams channel configuration for an agent."""
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")
    config = await get_channel_config_metadata(agent_id, ChannelType.MICROSOFT_TEAMS)
    if not config:
        raise HTTPException(status_code=404, detail="Microsoft Teams not configured")
    _ = await channel_config_dao.delete(record_id=config.id)


# ─── Event Webhook ──────────────────────────────────────


@router.post("/channel/teams/{agent_id}/webhook", response_model=None)
async def teams_event_webhook(agent_id: uuid.UUID, request: Request) -> Response | OkAcknowledgement:
    """Handle Microsoft Teams Bot Framework callbacks."""
    if await read_bounded_body(request, CHANNEL_WEBHOOK_MAX_BODY_SIZE) is None:
        return Response(status_code=413)

    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.MICROSOFT_TEAMS)
    if not config:
        return Response(status_code=404)
    try:
        activity = await authenticate_teams_activity(config, await request.body(), request.headers)
    except TeamsAuthenticationError:
        return Response(status_code=401)

    try:
        from_account = _json_mapping(activity.get("from"))
        extra = _extra_config(config)
        service_url = json_as_str(activity.get("serviceUrl"))
        if service_url not in (None, "") and extra.get("service_url") != service_url:
            extra["service_url"] = service_url
            config = (
                await channel_config_dao.update(
                    db_obj=config,
                    obj_in={"extra_config": extra, "is_connected": True},
                )
                or config
            )
            logger.info(f"Teams: Updated service_url for agent {agent_id} to {service_url}")

        activity_id = activity.get("id")
        if activity.get("type") != "message":
            return {"ok": True}

        recipient = _json_mapping(activity.get("recipient"))
        bot_id = config.app_id or json_as_str(recipient.get("id"))
        if bot_id not in (None, "") and from_account.get("id") == bot_id:
            return {"ok": True}

        user_text = str(activity.get("text", "") or "").strip()
        media = parse_teams_media(activity)
        if not user_text and not media.attachments:
            return {"ok": True}

        conversation = _json_mapping(activity.get("conversation"))
        conversation_id = json_as_str(conversation.get("id"))
        sender_id = json_as_str(from_account.get("id"))

        if conversation_id in (None, "") or sender_id in (None, ""):
            logger.warning(f"[Teams] Missing conversation_id or sender_id in activity for agent {agent_id}")
            return {"ok": True}

        logger.info("[Teams] Message from={}, conversation={}, chars={}", sender_id, conversation_id, len(user_text))

        if not bool(activity_id):
            return Response(status_code=400, content="Teams activity ID is required")
        from app.services.channels.submissions import ChannelEvent, accept_channel_event

        event = ChannelEvent(
            config_id=config.id,
            agent_id=agent_id,
            provider="microsoft_teams",
            event_key=json.dumps([config.app_id, conversation_id, str(activity_id)], separators=(",", ":")),
            sender_id=sender_id,
            conversation_id=conversation_id,
            text=user_text,
            media_ids=media.media_ids(),
            is_group=json_as_str_or(conversation.get("conversationType")) in ("groupChat", "channel"),
        )
        if media.attachments:
            await accept_teams_media(event, media, partial(_get_teams_access_token, config))
        else:
            await accept_channel_event(event)
        return {"ok": True}
    except HTTPException, StorageQuotaError:
        raise
    except Exception as e:
        logger.error("Teams: webhook handler failed agent={} error_type={}", agent_id, type(e).__name__)
        return Response(status_code=500, content="Internal server error")
