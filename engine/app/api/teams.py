"""Microsoft Teams Bot Channel API routes."""

import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path as _Path
from typing import TypedDict

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.config import get_settings
from app.core.json_types import JsonObject
from app.core.logging import logger
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.dao.channel_config_dao import channel_config_dao
from app.dao.user_dao import user_dao
from app.records.channel_config import ChannelConfigRecord
from app.records.user import UserRecord
from app.schemas.schemas import ChannelConfigOut
from app.services.agent_tool_exec.channel_context import channel_file_sender as _cfs_s
from app.services.channels import dedup as channel_dedup, inbound as channel_inbound

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

    use_managed_identity = (config.extra_config or {}).get("use_managed_identity", False)

    if use_managed_identity:
        try:
            from azure.core.credentials import AccessToken
            from azure.identity.aio import DefaultAzureCredential

            credential = DefaultAzureCredential()
            scope = "https://api.botframework.com/.default"
            token: AccessToken = await credential.get_token(scope)

            _teams_tokens[agent_id] = {
                "access_token": token.token,
                "expires_at": token.expires_on,
            }
            logger.info(
                f"Teams: Successfully obtained access token via managed identity for agent {agent_id}, expires at {token.expires_on}"
            )
            await credential.close()
            return token.token
        except ImportError:
            logger.error("Teams: azure-identity package not installed. Install it with: pip install azure-identity")
            return None
        except Exception as e:
            logger.exception(f"Teams: Failed to get access token via managed identity for agent {agent_id}: {e}")
            return None

    app_id = config.app_id
    app_secret = config.app_secret
    if not app_id or not app_secret:
        logger.error(f"Teams: Missing app_id or app_secret for agent {agent_id}")
        return None

    tenant_id = (config.extra_config or {}).get("tenant_id") or os.environ.get("TEAMS_TENANT_ID") or "common"
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
                    error_json = resp.json()
                    error_description = error_json.get("error_description", "No description")
                    error_code = error_json.get("error", "unknown")
                    logger.error(
                        f"Teams: OAuth token request failed for agent {agent_id}: status={resp.status_code}, error={error_code}, description={error_description}"
                    )
                except ValueError:
                    logger.error(
                        f"Teams: OAuth token request failed for agent {agent_id}: status={resp.status_code}, response={error_body[:500]}"
                    )
                logger.error(f"Teams: Token URL={token_url}, tenant_id={tenant_id}, client_id={app_id[:20]}...")
                return None
            token_data = resp.json()
            access_token = token_data["access_token"]
            expires_in = token_data["expires_in"]

            _teams_tokens[agent_id] = {
                "access_token": access_token,
                "expires_at": time.time() + expires_in,
            }
            logger.info(f"Teams: Successfully obtained access token for agent {agent_id}, expires in {expires_in}s")
            return access_token
    except httpx.HTTPStatusError as e:
        error_body = e.response.text if hasattr(e, "response") and e.response else "No response body"
        try:
            if hasattr(e, "response") and e.response:
                error_json = e.response.json()
                error_description = error_json.get("error_description", "No description")
                error_code = error_json.get("error", "unknown")
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


async def _send_teams_message(config: ChannelConfigRecord, conversation_id: str, activity: JsonObject) -> None:
    """Send an activity (message) to Microsoft Teams."""
    access_token = await _get_teams_access_token(config)
    if not access_token:
        logger.error(f"Teams: No access token for agent {config.agent_id}, cannot send message")
        raise ValueError("No access token available")

    service_url = (config.extra_config or {}).get("service_url")
    if not isinstance(service_url, str) or not service_url:
        logger.error(f"Teams: No service_url in config for agent {config.agent_id}, cannot send message")
        raise ValueError(f"No service_url in config for agent {config.agent_id}")

    if "type" not in activity:
        activity["type"] = "message"
    if "timestamp" not in activity:
        activity["timestamp"] = datetime.now(UTC).isoformat() + "Z"

    if activity.get("replyToId") and "id" not in activity:
        activity["id"] = str(uuid.uuid4())

    text_value = activity.get("text", "")
    text_content = text_value if isinstance(text_value, str) else ""
    if len(text_content.encode("utf-8")) > TEAMS_MSG_LIMIT:
        chunks = [text_content[i : i + TEAMS_MSG_LIMIT] for i in range(0, len(text_content), TEAMS_MSG_LIMIT)]
        for i, chunk in enumerate(chunks):
            chunk_activity = {**activity, "text": chunk}
            if i > 0:
                chunk_activity.pop("replyToId", None)
            await _send_teams_message_single_chunk(access_token, service_url, conversation_id, chunk_activity)
    else:
        await _send_teams_message_single_chunk(access_token, service_url, conversation_id, activity)


async def _send_teams_message_single_chunk(
    access_token: str, service_url: str, conversation_id: str, activity: JsonObject
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
                    error_json = resp.json()
                    error_description = error_json.get("error", {}).get(
                        "message", error_json.get("message", "No description")
                    )
                    error_code = error_json.get("error", {}).get("code", "unknown")
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
            resp.raise_for_status()
            logger.info(f"Teams: Sent message to conversation {conversation_id}")
    except httpx.HTTPStatusError as e:
        error_body = e.response.text if hasattr(e, "response") and e.response else "No response body"
        try:
            if hasattr(e, "response") and e.response:
                error_json = e.response.json()
                error_description = error_json.get("error", {}).get(
                    "message", error_json.get("message", "No description")
                )
                error_code = error_json.get("error", {}).get("code", "unknown")
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
):
    """Configure Microsoft Teams bot for an agent. Fields: app_id, app_secret."""
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    app_id = data.get("app_id", "").strip()
    app_secret = data.get("app_secret", "").strip()
    tenant_id = data.get("tenant_id", "").strip()
    use_managed_identity = data.get("use_managed_identity", False)

    if not use_managed_identity and (not app_id or not app_secret):
        raise HTTPException(
            status_code=422, detail="Either use_managed_identity must be enabled, or app_id and app_secret are required"
        )

    existing = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="microsoft_teams")
    if existing:
        extra = dict(existing.extra_config or {})
        if tenant_id:
            extra["tenant_id"] = tenant_id
        elif "tenant_id" in extra and not tenant_id:
            extra.pop("tenant_id", None)
        extra["use_managed_identity"] = use_managed_identity
        obj_in: dict = {
            "is_configured": True,
            "extra_config": extra,
        }
        if not use_managed_identity:
            obj_in["app_id"] = app_id
            obj_in["app_secret"] = app_secret
        config = await channel_config_dao.update(db_obj=existing, obj_in=obj_in)
        return ChannelConfigOut.model_validate(config or existing)

    extra_config: dict = {}
    if tenant_id:
        extra_config["tenant_id"] = tenant_id
    if use_managed_identity:
        extra_config["use_managed_identity"] = True

    config = await channel_config_dao.create(
        obj_in={
            "agent_id": agent_id,
            "channel_type": "microsoft_teams",
            "app_id": app_id if not use_managed_identity else None,
            "app_secret": app_secret if not use_managed_identity else None,
            "is_configured": True,
            "extra_config": extra_config,
        }
    )
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/teams-channel", response_model=ChannelConfigOut)
async def get_teams_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Get Microsoft Teams channel configuration for an agent."""
    await check_agent_access(current_user, agent_id)
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="microsoft_teams")
    if not config:
        raise HTTPException(status_code=404, detail="Microsoft Teams not configured")
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/teams-channel/webhook-url")
async def get_teams_webhook_url(
    agent_id: uuid.UUID, request: Request, current_user: UserRecord = Depends(get_current_user), db=None
):
    """Get the Microsoft Teams webhook URL for an agent."""
    await check_agent_access(current_user, agent_id)
    from app.services.platform_service import platform_service

    public_base = await platform_service.get_public_base_url(db, request)
    return {"webhook_url": f"{public_base}/api/channel/teams/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/teams-channel", status_code=204)
async def delete_teams_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Delete Microsoft Teams channel configuration for an agent."""
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="microsoft_teams")
    if not config:
        raise HTTPException(status_code=404, detail="Microsoft Teams not configured")
    await channel_config_dao.delete(id=config.id)


# ─── Event Webhook ──────────────────────────────────────

@router.post("/channel/teams/{agent_id}/webhook")
async def teams_event_webhook(agent_id: uuid.UUID, request: Request):
    """Handle Microsoft Teams Bot Framework callbacks."""
    try:
        body_bytes = await request.body()
        try:
            body = json.loads(body_bytes)
        except json.JSONDecodeError as e:
            logger.error(f"Teams: Failed to parse JSON body: {e}, body={body_bytes[:200]}")
            return Response(status_code=400, content="Invalid JSON")

        if isinstance(body, dict) and "type" in body:
            activity = body
        elif isinstance(body, dict) and "activity" in body:
            activity = body["activity"]
        else:
            logger.warning(
                f"Teams: Unexpected body structure for agent {agent_id}: {list(body.keys()) if isinstance(body, dict) else type(body)}"
            )
            activity = body if isinstance(body, dict) else {}

        logger.info(
            f"Teams: Webhook received for agent {agent_id}, activity type={activity.get('type')}, from={activity.get('from', {}).get('id', 'unknown')}, text={activity.get('text', '')[:50] if activity.get('text') else 'no text'}"
        )

        config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="microsoft_teams")
        if not config:
            logger.warning(f"Teams: Webhook received for unconfigured agent {agent_id}")
            return Response(status_code=404)

        service_url = activity.get("serviceUrl")
        if service_url and (config.extra_config or {}).get("service_url") != service_url:
            extra = dict(config.extra_config or {})
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
        if activity_id and channel_dedup.already_processed("teams", str(activity_id), cap=2000):
            return {"ok": True}

        if activity.get("type") != "message":
            return {"ok": True}

        bot_id = config.app_id
        if not bot_id:
            bot_id = activity.get("recipient", {}).get("id")
        if bot_id and activity.get("from", {}).get("id") == bot_id:
            return {"ok": True}

        user_text = activity.get("text", "").strip()
        if not user_text:
            return {"ok": True}

        conversation_id = activity.get("conversation", {}).get("id")
        sender_id = activity.get("from", {}).get("id")
        sender_name = activity.get("from", {}).get("name", f"Teams User {sender_id[:8]}")
        reply_to_id = activity.get("id")

        if not conversation_id or not sender_id:
            logger.warning(f"[Teams] Missing conversation_id or sender_id in activity for agent {agent_id}")
            return {"ok": True}

        logger.info(f"[Teams] Message from={sender_id}, conversation={conversation_id}: {user_text[:80]}")

        agent_obj = await channel_inbound.load_agent(agent_id)
        if not agent_obj:
            logger.warning(f"[Teams] Agent {agent_id} not found")
            return {"ok": True}

        _extra_info = {"name": sender_name}
        platform_user = await channel_inbound.resolve_sender_user(
            agent=agent_obj,
            channel_type="teams",
            external_user_id=sender_id,
            extra_info=_extra_info,
        )

        if (
            sender_name
            and platform_user.display_name
            and platform_user.display_name.startswith("Teams User ")
            and sender_name != platform_user.display_name
        ):
            platform_user = (
                await user_dao.update(db_obj=platform_user, obj_in={"display_name": sender_name}) or platform_user
            )

        _conv_type = activity.get("conversation", {}).get("conversationType", "")
        _is_group_teams = _conv_type in ("groupChat", "channel")

        sess = await channel_inbound.open_channel_session(
            agent_id=agent_id,
            user_id=platform_user.id if not _is_group_teams else (agent_obj.creator_id or platform_user.id),
            external_conv_id=conversation_id,
            source_channel="microsoft_teams",
            first_message_title=user_text,
            is_group=_is_group_teams,
            group_name=activity.get("conversation", {}).get("name")
            or (f"Teams Group {conversation_id[:8]}" if _is_group_teams else None),
        )

        async def _teams_file_sender(file_path, msg: str = ""):
            _fp = _Path(file_path)
            use_mi = (config.extra_config or {}).get("use_managed_identity", False)
            has_creds = (config.app_id and config.app_secret) or use_mi
            if not has_creds or not conversation_id:
                return
            file_msg_activity: JsonObject = {
                "type": "message",
                "conversation": {"id": conversation_id},
                "replyToId": reply_to_id,
                "text": (
                    f"Agent sent file: {_fp.name} "
                    f"(Note: file content not directly supported yet, but I can tell you about it: {msg})"
                ),
            }
            await _send_teams_message(config, conversation_id, file_msg_activity)

        _cfs_s_token = _cfs_s.set(_teams_file_sender)

        try:
            reply_text = await channel_inbound.run_text_turn(
                agent=agent_obj,
                agent_id=agent_id,
                platform_user=platform_user,
                session=sess,
                user_text=user_text,
            )
            logger.info(f"[Teams] LLM reply generated: {reply_text[:80]}")
        except Exception as e:
            logger.exception(f"[Teams] Failed to call LLM for agent {agent_id}: {e}")
            reply_text = "Sorry, I encountered an error processing your message."
            try:
                await channel_inbound.persist_assistant_message(
                    agent_id=agent_id,
                    user_id=platform_user.id,
                    session=sess,
                    content=reply_text,
                    agent=agent_obj,
                )
            except Exception:
                logger.exception("[Teams] Failed to persist error reply")
        finally:
            _cfs_s.reset(_cfs_s_token)

        use_managed_identity = (config.extra_config or {}).get("use_managed_identity", False)
        has_credentials = (config.app_id and config.app_secret) or use_managed_identity
        if has_credentials and conversation_id:
            try:
                bot_channel_account = activity.get("recipient", {})
                if not bot_channel_account.get("id"):
                    if config.app_id:
                        bot_channel_account = {"id": config.app_id}
                    else:
                        logger.error(
                            "[Teams] Cannot determine bot channel account ID - no recipient in activity and no app_id configured"
                        )
                        raise ValueError("Cannot determine bot channel account ID")

                user_account = activity.get("from", {})
                if not user_account.get("id"):
                    user_account = {"id": sender_id, "name": sender_name}

                reply_activity: JsonObject = {
                    "type": "message",
                    "from": bot_channel_account,
                    "conversation": {"id": conversation_id},
                    "recipient": user_account,
                    "replyToId": reply_to_id,
                    "text": reply_text,
                }
                logger.info(
                    f"[Teams] Attempting to send reply to conversation {conversation_id}, "
                    f"from={bot_channel_account.get('id')}, recipient={user_account.get('id')}"
                )
                await _send_teams_message(config, conversation_id, reply_activity)
                logger.info("[Teams] Successfully sent reply to Teams")
            except Exception as e:
                logger.exception(f"[Teams] Failed to send message to Teams: {e}")
        else:
            use_mi = (config.extra_config or {}).get("use_managed_identity", False)
            logger.warning(
                f"[Teams] Cannot send reply - missing credentials "
                f"(managed_identity={use_mi}, app_id={bool(config.app_id)}, "
                f"app_secret={bool(config.app_secret)}), conversation_id={bool(conversation_id)}"
            )

        if activity_id:
            channel_dedup.mark_processed("teams", str(activity_id), cap=2000)

        return {"ok": True}
    except Exception as e:
        logger.exception(f"Teams: Unhandled exception in webhook handler for agent {agent_id}: {e}")
        return Response(status_code=500, content="Internal server error")
