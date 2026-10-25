"""Microsoft Teams Bot Channel API routes."""

import json
import os
import time
import uuid
from datetime import UTC, datetime
from typing import TypedDict

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
from app.services.channels.teams_auth import (
    TeamsAuthenticationError,
    authenticate_teams_activity,
    is_bot_framework_service_url,
)

settings = get_settings()

router = APIRouter(tags=["microsoft_teams"])

TEAMS_MSG_LIMIT = 28000  # Teams message char limit (approx 28KB)
DEFAULT_CONTEXT_WINDOW_SIZE = 100


class TeamsAccessToken(TypedDict):
    access_token: str
    expires_at: float


class TeamsChannelPayload(TypedDict, total=False):
    app_id: str
    app_secret: str
    tenant_id: str
    use_managed_identity: bool


# In-memory cache for OAuth tokens
_teams_tokens: dict[str, TeamsAccessToken] = {}  # agent_id -> {access_token, expires_at}


def _extra_config(config: ChannelConfigRecord) -> JsonObject:
    return json_object_from(config.extra_config)


def _json_mapping(value: object) -> JsonObject:
    return json_object_from(value)


def _oauth_error_fields(payload: object) -> tuple[str, str]:
    data = json_object_from(payload)
    nested = data.get("error")
    if is_json_object(nested):
        code = json_as_str_or(nested.get("code"), "unknown")
        description = json_as_str_or(nested.get("message"), json_as_str_or(data.get("message"), "No description"))
        return code, description
    error_code = json_as_str_or(data.get("error"), "unknown")
    fallback_message = json_as_str_or(data.get("message"), "No description")
    error_description = json_as_str_or(data.get("error_description"), fallback_message)
    return error_code, error_description


async def _get_teams_access_token(config: ChannelConfigRecord) -> str | None:
    """Get or refresh Microsoft Teams access token.

    Supports:
    - Client credentials (app_id + app_secret) - default
    - Managed Identity (when use_managed_identity is True in extra_config)
    """
    agent_id = str(config.agent_id)
    cached = _teams_tokens.get(agent_id)
    if cached and cached["expires_at"] > time.time() + 60:
        logger.debug(f"Teams: Using cached access token for agent {agent_id}")
        return cached["access_token"]

    extra = _extra_config(config)
    use_managed_identity = bool(extra.get("use_managed_identity", False))

    if use_managed_identity:
        try:
            from typing import Protocol, cast

            class _AzureToken(Protocol):
                token: object
                expires_on: object

            class _AzureCredential(Protocol):
                async def get_token(self, scope: str) -> _AzureToken: ...

                async def close(self) -> object: ...

            class _AzureIdentityAio(Protocol):
                DefaultAzureCredential: type[_AzureCredential]

            import importlib

            azure_identity = cast("_AzureIdentityAio", importlib.import_module("azure.identity.aio"))
            credential = azure_identity.DefaultAzureCredential()
            scope = "https://api.botframework.com/.default"
            token = await credential.get_token(scope)
            access_token_raw: object = token.token
            expires_on_raw: object = token.expires_on
            if not isinstance(access_token_raw, str) or not isinstance(expires_on_raw, (int, float)):
                return None

            _teams_tokens[agent_id] = {
                "access_token": access_token_raw,
                "expires_at": float(expires_on_raw),
            }
            logger.info(
                f"Teams: Successfully obtained access token via managed identity for agent {agent_id}, expires at {expires_on_raw}"
            )
            _ = await credential.close()
            return access_token_raw
        except ImportError:
            logger.error("Teams: azure-identity package not installed. Install it with: pip install azure-identity")
            return None
        except Exception as e:
            logger.exception(f"Teams: Failed to get access token via managed identity for agent {agent_id}: {e}")
            return None

    app_id = config.app_id
    app_secret = config.app_secret
    if app_id in (None, "") or app_secret in (None, ""):
        logger.error(f"Teams: Missing app_id or app_secret for agent {agent_id}")
        return None

    tenant_raw = extra.get("tenant_id")
    if isinstance(tenant_raw, str) and tenant_raw:
        tenant_id = tenant_raw
    else:
        tenant_id = os.environ.get("TEAMS_TENANT_ID") or "common"
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": app_id,
        "client_secret": app_secret,
        "grant_type": "client_credentials",
        "scope": "https://api.botframework.com/.default",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(token_url, data=data)
            if resp.status_code != 200:
                error_body = resp.text
                try:
                    error_code, error_description = _oauth_error_fields(json_object_from_response(resp))
                    logger.error(
                        f"Teams: OAuth token request failed for agent {agent_id}: status={resp.status_code}, error={error_code}, description={error_description}"
                    )
                except ValueError:
                    logger.error(
                        f"Teams: OAuth token request failed for agent {agent_id}: status={resp.status_code}, response={error_body[:500]}"
                    )
                logger.error(f"Teams: Token URL={token_url}, tenant_id={tenant_id}, client_id={app_id[:20]}...")
                return None
            token_data = json_object_from_response(resp)
            access_token = token_data.get("access_token")
            expires_in = token_data.get("expires_in")
            if not isinstance(access_token, str) or not isinstance(expires_in, (int, float)):
                return None

            _teams_tokens[agent_id] = {
                "access_token": access_token,
                "expires_at": time.time() + float(expires_in),
            }
            logger.info(f"Teams: Successfully obtained access token for agent {agent_id}, expires in {expires_in}s")
            return access_token
    except httpx.HTTPStatusError as e:
        error_body = e.response.text if hasattr(e, "response") and e.response else "No response body"
        try:
            if hasattr(e, "response") and e.response:
                error_code, error_description = _oauth_error_fields(json_object_from_response(e.response))
                logger.error(
                    f"Teams: OAuth token HTTP error for agent {agent_id}: status={e.response.status_code}, error={error_code}, description={error_description}"
                )
        except ValueError:
            logger.error(
                f"Teams: OAuth token HTTP error for agent {agent_id}: status={e.response.status_code if hasattr(e, 'response') and e.response else 'unknown'}, response={error_body[:500]}"
            )
        logger.error(f"Teams: Token URL={token_url}, tenant_id={tenant_id}, client_id={app_id[:20]}...")
        return None
    except Exception as e:
        logger.exception(f"Teams: Failed to get access token for agent {agent_id}: {e}")
        return None


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
            await _send_teams_message_single_chunk(
                access_token, service_url, conversation_id, chunk_activity, strict=strict
            )
    else:
        await _send_teams_message_single_chunk(access_token, service_url, conversation_id, activity, strict=strict)


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
            if resp.status_code != 200:
                error_body = resp.text
                try:
                    error_code, error_description = _oauth_error_fields(json_object_from_response(resp))
                    logger.error(
                        f"Teams: Failed to send message: status={resp.status_code}, error={error_code}, description={error_description}"
                    )
                except ValueError:
                    logger.error(
                        f"Teams: Failed to send message: status={resp.status_code}, response={error_body[:500]}"
                    )
                logger.error(
                    f"Teams: POST URL={post_url}, conversation_id={conversation_id}, service_url={service_url}"
                )
            _ = resp.raise_for_status()
            if strict and not bool(json_as_str(json_object_from_response(resp).get("id"))):
                raise RuntimeError("Teams did not acknowledge the message")
            logger.info(f"Teams: Sent message to conversation {conversation_id}")
    except httpx.HTTPStatusError as e:
        error_body = e.response.text if hasattr(e, "response") and e.response else "No response body"
        try:
            if hasattr(e, "response") and e.response:
                error_code, error_description = _oauth_error_fields(json_object_from_response(e.response))
                logger.error(
                    f"Teams: HTTP error sending message: status={e.response.status_code}, error={error_code}, description={error_description}"
                )
        except ValueError:
            logger.error(
                f"Teams: HTTP error sending message: status={e.response.status_code if hasattr(e, 'response') and e.response else 'unknown'}, response={error_body[:500]}"
            )
        logger.error(f"Teams: POST URL={post_url}, conversation_id={conversation_id}, service_url={service_url}")
        raise


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

    existing = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.MICROSOFT_TEAMS)
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
        return ChannelConfigOut.model_validate(config or existing)

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
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/teams-channel", response_model=ChannelConfigOut)
async def get_teams_channel(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> ChannelConfigOut:
    """Get Microsoft Teams channel configuration for an agent."""
    _ = await check_agent_access(current_user, agent_id)
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.MICROSOFT_TEAMS)
    if not config:
        raise HTTPException(status_code=404, detail="Microsoft Teams not configured")
    return ChannelConfigOut.model_validate(config)


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
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.MICROSOFT_TEAMS)
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
        if not user_text:
            return {"ok": True}

        conversation = _json_mapping(activity.get("conversation"))
        conversation_id = json_as_str(conversation.get("id"))
        sender_id = json_as_str(from_account.get("id"))

        if conversation_id in (None, "") or sender_id in (None, ""):
            logger.warning(f"[Teams] Missing conversation_id or sender_id in activity for agent {agent_id}")
            return {"ok": True}

        logger.info(f"[Teams] Message from={sender_id}, conversation={conversation_id}: {user_text[:80]}")

        if not bool(activity_id):
            return Response(status_code=400, content="Teams activity ID is required")
        from app.services.channels.submissions import ChannelEvent, accept_channel_event

        await accept_channel_event(
            ChannelEvent(
                config_id=config.id,
                agent_id=agent_id,
                provider="microsoft_teams",
                event_key=json.dumps([config.app_id, conversation_id, str(activity_id)], separators=(",", ":")),
                sender_id=sender_id,
                conversation_id=conversation_id,
                text=user_text,
                is_group=json_as_str_or(conversation.get("conversationType")) in ("groupChat", "channel"),
            )
        )
        return {"ok": True}
    except Exception as e:
        logger.exception(f"Teams: Unhandled exception in webhook handler for agent {agent_id}: {e}")
        return Response(status_code=500, content="Internal server error")
