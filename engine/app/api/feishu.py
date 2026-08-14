"""Feishu OAuth and Channel API routes."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import TypedDict, TypeIs

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from app.core.json_types import (
    JsonObject,
    JsonValue,
    json_as_str,
    json_as_str_or,
    json_loads_object,
    json_object_from_response,
    mapping_from_row,
    object_list_from_row,
)
from app.core.logging import logger
from app.core.permissions import check_agent_access, is_agent_creator, is_agent_expired
from app.core.security import get_current_user
from app.dao.agent_dao import agent_dao
from app.dao.channel_config_dao import channel_config_dao
from app.dao.identity_provider_dao import identity_provider_dao
from app.dao.sso_scan_session_dao import sso_scan_session_dao
from app.dao.task_dao import task_dao
from app.records.agent import AgentRecord
from app.records.channel_config import ChannelConfigRecord
from app.records.chat import ChatMessageRecord
from app.records.llm import LLMModelRecord
from app.records.user import UserRecord
from app.schemas.schemas import ChannelConfigCreate, ChannelConfigOut, TokenResponse, UserOut
from app.services.channels import dedup as channel_dedup, inbound as channel_inbound
from app.services.feishu_service import feishu_service
from app.services.llm.base import ChunkCallback, ThinkingCallback, ToolCallback, ToolCallbackData
from app.services.llm.turn import TurnContext
from app.services.llm.types import OpenAIMessage
from app.services.llm.utils import truncate_messages_with_pair_integrity
from app.services.storage import agent_upload_key, get_storage_backend, store_agent_upload

router = APIRouter(tags=["feishu"])

_background_tasks: set[asyncio.Task[None]] = set()
DEFAULT_CONTEXT_WINDOW_SIZE = 100


def _is_json_object(value: object) -> TypeIs[JsonObject]:
    return isinstance(value, dict)


def _json_object(value: object) -> JsonObject:
    return value if _is_json_object(value) else mapping_from_row(value)


def _response_json_object(resp: httpx.Response) -> JsonObject:
    return json_object_from_response(resp)


def _app_access_token(resp: httpx.Response) -> str:
    return json_as_str_or(_response_json_object(resp).get("app_access_token"), "")


def _avatar_url_from_user(user_info: JsonObject) -> str:
    raw_avatar: object = user_info.get("avatar")
    if isinstance(raw_avatar, dict):
        avatar = _json_object(raw_avatar)
        return (
            json_as_str_or(avatar.get("avatar_240"), "")
            or json_as_str_or(avatar.get("avatar_640"), "")
            or json_as_str_or(avatar.get("avatar_origin"), "")
        )
    return json_as_str_or(raw_avatar, "")


def _schedule_background(coroutine: Awaitable[object]) -> None:
    async def run() -> None:
        await coroutine

    task = asyncio.create_task(run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# Default LLM timeout for Feishu channel (fallback when model has no request_timeout set).
# The per-model request_timeout field takes precedence - see _get_llm_timeout().
_LLM_TIMEOUT_SECONDS_DEFAULT = 180.0

# Number of tool status lines to keep visible in the Feishu card.
# Shows the last N non-running lines plus any active "running" entry.
_TOOL_STATUS_KEEP_LINES = 20

_USER_RESOLUTION_ERROR_TIP = (
    "Sorry, we could not reliably identify your Feishu account. This request was stopped to avoid creating a duplicate account. "
    + "Please try again later or ask an administrator to check Feishu Contact API permissions."
)


class FeishuToolEvent(TypedDict, total=False):
    name: str
    call_id: str
    status: str
    result: JsonValue
    args: JsonObject
    arguments: JsonObject
    reasoning_content: str


def _storage_mtime(entry: object) -> float:
    mtime_raw: object = getattr(entry, "modified_at", "")
    raw = str(mtime_raw or "")
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0


def _build_card(
    answer_text: str,
    thinking_text: str = "",
    streaming: bool = False,
    tool_status_lines: list[str] | None = None,
    agent_name: str = "AI Reply",
) -> JsonObject:
    """Build a Feishu interactive card for streaming replies."""
    elements: list[JsonValue] = []

    if tool_status_lines:
        elements.append(
            {
                "tag": "markdown",
                "content": "\n".join(tool_status_lines[-_TOOL_STATUS_KEEP_LINES:]),
            }
        )
        elements.append({"tag": "hr"})

    if thinking_text:
        think_preview = thinking_text[:200].replace("\n", " ")
        elements.append(
            {
                "tag": "markdown",
                "content": f"<font color='grey'>💭 **Thinking**\n{think_preview}{'...' if len(thinking_text) > 200 else ''}</font>",
            }
        )
        elements.append({"tag": "hr"})

    body = answer_text + ("▌" if streaming and answer_text else ("..." if streaming else ""))
    elements.append({"tag": "markdown", "content": body or "..."})
    return {
        "config": {"update_multi": True},
        "header": {
            "template": "blue",
            "title": {"content": agent_name, "tag": "plain_text"},
        },
        "elements": elements,
    }


def _message_id_from_send_response(response: JsonObject) -> str | None:
    data = response.get("data")
    if not isinstance(data, dict):
        return None
    message_id = data.get("message_id")
    return message_id if isinstance(message_id, str) else None


def _looks_like_error_text(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    markers = (
        "⚠️",
        "[error]",
        "error:",
        "error ",
        "failed",
        "failure",
        "timeout",
        "timed out",
        "调用模型出错",
        "未配置 llm 模型",
        "数字员工未找到",
    )
    return any(marker in normalized for marker in markers)


def _normalize_tool_error(tool_name: str, result: object) -> str | None:
    text = "" if result is None else str(result).strip()
    if not text or not _looks_like_error_text(text):
        return None
    compact = " ".join(text.split())
    if len(compact) > 240:
        compact = compact[:240].rstrip() + "..."
    return f"`{tool_name}`: {compact}"


def _append_error_details(reply_text: str, tool_errors: list[str]) -> str:
    base = (reply_text or "").strip()
    unique_errors: list[str] = []
    seen: set[str] = set()
    for item in tool_errors:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_errors.append(normalized)
    if not unique_errors:
        return base
    details = "\n".join(f"- {item}" for item in unique_errors)
    if not base:
        return f"Execution failed with the following errors:\n{details}"
    if _looks_like_error_text(base):
        return f"{base}\n\nThe following errors occurred during execution:\n{details}"
    return base


def _normalize_history_messages(history: list[OpenAIMessage] | None) -> list[OpenAIMessage]:
    """Drop UI-only message roles before replaying history into the LLM."""
    if not history:
        return []
    allowed_roles = {"system", "assistant", "user", "tool", "function"}
    normalized: list[OpenAIMessage] = []
    for msg in history:
        role = msg.get("role")
        if role not in allowed_roles:
            continue
        normalized.append(msg)
    return normalized


def _get_llm_timeout(model: LLMModelRecord) -> float:
    """Get effective LLM timeout for the Feishu channel.

    Prefer the model-level request_timeout so each model can have its own
    budget (local vLLM may need 300 s, cloud APIs often need only 60 s).
    Falls back to _LLM_TIMEOUT_SECONDS_DEFAULT when the field is absent or zero.
    """
    timeout = model.request_timeout
    if timeout and timeout > 0:
        return float(timeout)
    return _LLM_TIMEOUT_SECONDS_DEFAULT


class _SerialPatchQueue:
    """Serialize patch requests for one Feishu message to prevent out-of-order overwrite."""

    def __init__(self) -> None:
        self._tail: asyncio.Task[None] | None = None

    def enqueue(self, job_factory: Callable[[], Awaitable[None]]) -> None:
        prev = self._tail

        async def _runner() -> None:
            if prev:
                try:
                    await prev
                except Exception as e:
                    logger.warning(f"[Feishu] Previous patch job failed before next job: {e}")
            await job_factory()

        self._tail = asyncio.create_task(_runner())

    async def drain(self) -> None:
        if self._tail:
            await self._tail


def _build_llm_history_from_chat_messages(history_messages: Iterable[ChatMessageRecord]) -> list[OpenAIMessage]:
    """Rebuild LLM history from persisted chat messages.

    Feishu persists real tool calls as `tool_call` rows. To preserve the
    same conversational continuity as the web client, convert those rows back
    into assistant tool-call messages plus tool result messages before sending
    history to the model.
    """
    import json as _json

    history: list[OpenAIMessage] = []
    for msg in history_messages:
        if msg.role == "tool_call":
            try:
                payload = json_loads_object(msg.content or "{}")
            except Exception:
                payload = {}

            # Support both local schema (tool_name/arguments) and remote schema (name/args)
            tool_name = json_as_str_or(payload.get("tool_name") or payload.get("name"), "unknown_tool")
            tool_args_raw: object = payload.get("arguments") or payload.get("args") or {}
            tool_result_raw: object = payload.get("result") or ""
            call_id = json_as_str_or(payload.get("tool_call_id"), f"feishu-tool-{msg.id}")

            history.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": _json.dumps(_json_object(tool_args_raw), ensure_ascii=False)
                                if isinstance(tool_args_raw, dict)
                                else str(tool_args_raw),
                            },
                        }
                    ],
                }
            )
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": str(tool_result_raw) if tool_result_raw else "",
                }
            )
            continue

        match msg.role:
            case "system" | "assistant" | "user" | "tool" as role:
                history.append({"role": role, "content": msg.content})
    return history


async def _save_feishu_tool_call(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    tool_name: str,
    status: str,
    arguments: JsonObject | None,
    result: str,
    tool_call_id: str | None = None,
    reasoning_content: str | None = None,
) -> None:
    """Persist a completed Feishu tool call into chat history."""
    from app.services.chat_session_service import save_tool_call_log

    await save_tool_call_log(
        agent_id=agent_id,
        user_id=user_id,
        conversation_id=conversation_id,
        tool_name=tool_name,
        arguments=arguments,
        result=result,
        status=status,
        tool_call_id=tool_call_id,
        reasoning_content=reasoning_content,
    )


# ─── OAuth ──────────────────────────────────────────────


@router.get("/auth/feishu/callback")
@router.post("/auth/feishu/callback", response_model=TokenResponse)
async def feishu_oauth_callback(code: str, state: str | None = None):
    """Handle Feishu OAuth callback - exchange code for user session."""
    from app.config import get_settings
    from app.core.security import create_access_token
    from app.services.auth_provider import FeishuAuthProvider

    tenant_id = None
    if state:
        try:
            sid = uuid.UUID(state)
            session = await sso_scan_session_dao.get(sid)
            if session:
                tenant_id = session.tenant_id
        except ValueError, AttributeError:
            pass

    try:
        settings = get_settings()
        feishu_config: JsonObject = {
            "app_id": settings.FEISHU_APP_ID,
            "app_secret": settings.FEISHU_APP_SECRET,
        }

        provider = None
        if tenant_id:
            provider = await identity_provider_dao.get_by_type_and_tenant("feishu", tenant_id)

        auth_provider = FeishuAuthProvider(provider=provider, config=feishu_config)

        tenant_id_str = str(tenant_id) if tenant_id else None
        _ = await auth_provider._ensure_provider(tenant_id_str)

        token_data = await auth_provider.exchange_code_for_token(code)
        access_token = token_data.get("access_token", "")
        if not isinstance(access_token, str):
            raise ValueError("Feishu token exchange did not return an access token")
        user_info = await auth_provider.get_user_info(access_token)

        user, _ = await auth_provider.find_or_create_user(user_info, tenant_id=tenant_id_str)

        token = create_access_token(str(user.id), user.role)

    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Feishu auth failed: {e}") from e

    if state:
        try:
            sid = uuid.UUID(state)
            session = await sso_scan_session_dao.get(sid)
            if session:
                _ = await sso_scan_session_dao.update(
                    db_obj=session,
                    obj_in={
                        "status": "authorized",
                        "provider_type": "feishu",
                        "user_id": user.id,
                        "access_token": token,
                        "error_msg": None,
                    },
                )
                return HTMLResponse(
                    f"""<html><head><meta charset="utf-8" /></head>
                    <body style="font-family: sans-serif; padding: 24px;">
                        <div>SSO login successful. Redirecting...</div>
                        <script>window.location.href = "/sso/entry?sid={sid}&complete=1";</script>
                    </body></html>"""
                )
        except Exception as e:
            logger.exception("Failed to update SSO session (feishu) %s", e)

    return TokenResponse(access_token=token, user=UserOut.model_validate(user))


# ─── Channel Config (per-agent Feishu bot) ──────────────


@router.post("/agents/{agent_id}/channel", response_model=ChannelConfigOut, status_code=status.HTTP_201_CREATED)
async def configure_channel(
    agent_id: uuid.UUID, data: ChannelConfigCreate, current_user: UserRecord = Depends(get_current_user)
):
    """Configure Feishu bot credentials for a digital employee (wizard step 5)."""
    agent, _access = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    extra_config = data.extra_config or {}
    existing = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="feishu")
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

        from app.services.feishu_ws import feishu_ws_manager

        mode = json_as_str_or((config.extra_config or {}).get("connection_mode"), "webhook")
        if mode == "websocket":
            _schedule_background(feishu_ws_manager.start_client(agent_id, data.app_id, data.app_secret))
        else:
            _schedule_background(feishu_ws_manager.stop_client(agent_id))

        return ChannelConfigOut.model_validate(config)

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

    from app.services.feishu_ws import feishu_ws_manager

    mode = json_as_str_or((config.extra_config or {}).get("connection_mode"), "webhook")
    if mode == "websocket":
        _schedule_background(feishu_ws_manager.start_client(agent_id, data.app_id, data.app_secret))

    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/channel", response_model=ChannelConfigOut)
async def get_channel_config(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Get Feishu channel configuration for an agent."""
    _ = await check_agent_access(current_user, agent_id)
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="feishu")
    if not config:
        raise HTTPException(status_code=404, detail="Channel not configured")
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/channel/webhook-url")
async def get_webhook_url(agent_id: uuid.UUID, request: Request, db: object | None = None) -> dict[str, str]:
    """Get the webhook URL for this agent's Feishu bot."""
    from app.services.platform_service import platform_service

    public_base = await platform_service.get_public_base_url(db, request)
    return {"webhook_url": f"{public_base}/api/channel/feishu/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/channel", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel_config(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Remove Feishu bot configuration for an agent."""
    agent, _access = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="feishu")
    if not config:
        raise HTTPException(status_code=404, detail="Channel not configured")
    _ = await channel_config_dao.delete(id=config.id)


# ─── Feishu Event Webhook ───────────────────────────────

# Simple in-memory dedup to avoid processing retried events


@router.post("/channel/feishu/{agent_id}/webhook")
async def feishu_event_webhook(
    agent_id: uuid.UUID,
    request: Request,
) -> JsonObject:
    """Handle Feishu event callback for a specific agent's bot."""
    body = json_loads_object(await request.body())

    # Handle verification challenge
    if "challenge" in body:
        return {"challenge": body["challenge"]}

    return await process_feishu_event(agent_id, body)


async def process_feishu_event(agent_id: uuid.UUID, body: JsonObject) -> JsonObject:
    """Core logic to process feishu events from both webhook and WS client.

    Manages its own short-lived database transactions to avoid holding connections
    open during slow LLM/HTTP operations.
    """
    import json as _json

    header = _json_object(body.get("header"))
    logger.info(
        f"[Feishu] Event processing for {agent_id}: event_type={json_as_str_or(header.get('event_type'), 'N/A')}"
    )

    # Deduplicate - Feishu retries on slow responses.
    # Mark only after successful handling so retries work on crash.
    event_id = json_as_str_or(header.get("event_id"), "")
    if event_id and await channel_dedup.already_processed_shared("feishu", event_id, cap=2000):
        return {"code": 0, "msg": "already processed"}

    # ── Phase 1: Load config + agent/model for LLM (pure-psycopg DAOs) ──
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="feishu")
    _agent_model, _llm_model, _fallback_model = await _load_agent_and_model(None, agent_id)
    if not config:
        return {"code": 1, "msg": "Channel not found"}
    app_id = config.app_id
    app_secret = config.app_secret
    if not app_id or not app_secret:
        logger.error(f"[Feishu] Channel credentials missing for agent {agent_id}")
        return {"code": 1, "msg": "Channel credentials not configured"}

    # Handle events
    event = _json_object(body.get("event"))
    event_type = json_as_str_or(header.get("event_type"), "")

    if event_type == "im.message.receive_v1":
        message = _json_object(event.get("message"))
        sender = _json_object(_json_object(event.get("sender")).get("sender_id"))
        sender_open_id = json_as_str_or(sender.get("open_id"), "")
        # tenant-stable ID, available directly in event body
        sender_user_id_from_event = json_as_str_or(sender.get("user_id"), "")
        msg_type = json_as_str_or(message.get("message_type"), "text")
        chat_type = json_as_str_or(message.get("chat_type"), "p2p")  # p2p or group
        chat_id = json_as_str_or(message.get("chat_id"), "")

        logger.info(
            f"[Feishu] Received {msg_type} message, chat_type={chat_type}, open_id={sender_open_id!r}, user_id_from_event={sender_user_id_from_event!r}"
        )

        # ── Normalize post (rich text) → extract text + schedule image downloads ──
        if msg_type == "post":
            import json as _json_post

            _post_body = json_loads_object(json_as_str_or(message.get("content"), "{}"))
            # Feishu post content: {"title": "...", "content": [[{"tag":"text","text":"..."},...],...]}
            # The content may be nested under a locale key like "zh_cn"
            _paragraphs_raw: object = _post_body.get("content", [])
            if not _paragraphs_raw:
                # Try locale keys (zh_cn, en_us, etc.)
                for _locale_val in _post_body.values():
                    if isinstance(_locale_val, dict) and "content" in _locale_val:
                        _paragraphs_raw = _locale_val["content"]
                        break
            _text_parts: list[str] = []
            _post_image_keys: list[str] = []
            _paragraphs = _paragraphs_raw if isinstance(_paragraphs_raw, list) else []
            for _para in _paragraphs:
                if not isinstance(_para, list):
                    continue
                _line_parts: list[str] = []
                for _elem_raw in _para:
                    _elem = _json_object(_elem_raw)
                    _tag = json_as_str_or(_elem.get("tag"), "")
                    if _tag == "text":
                        _line_parts.append(json_as_str_or(_elem.get("text"), ""))
                    elif _tag == "a":
                        _href = json_as_str_or(_elem.get("href"), "")
                        _link_text = json_as_str_or(_elem.get("text"), "")
                        _line_parts.append(f"{_link_text} ({_href})" if _href else _link_text)
                    elif _tag == "img":
                        _ik = json_as_str(_elem.get("image_key"))
                        if _ik:
                            _post_image_keys.append(_ik)
                if _line_parts:
                    _text_parts.append("".join(_line_parts))
            _extracted_text = "\n".join(_text_parts).strip()
            # Download images and embed as base64 for vision-capable models
            _image_markers: list[str] = []
            if _post_image_keys:
                import base64 as _b64

                _msg_id = json_as_str_or(message.get("message_id"), "")
                for _ik in _post_image_keys:
                    try:
                        _img_bytes = await feishu_service.download_message_resource(
                            app_id, app_secret, _msg_id, _ik, "image"
                        )
                        _, _workspace_path, _save_path = await store_agent_upload(
                            agent_id,
                            f"image_{_ik[-8:]}.jpg",
                            _img_bytes,
                            content_type="image/jpeg",
                        )
                        logger.info(f"[Feishu] Saved post image to {_workspace_path} ({len(_img_bytes)} bytes)")
                        # Embed as base64 marker for vision models
                        _b64_data = _b64.b64encode(_img_bytes).decode("ascii")
                        _image_markers.append(f"[image_data:data:image/jpeg;base64,{_b64_data}]")
                    except Exception as _dl_err:
                        logger.error(f"[Feishu] Failed to download post image {_ik}: {_dl_err}")
            # Build final text with embedded images
            if not _extracted_text and _image_markers:
                _extracted_text = "[用户发送了图片，请看图片内容]"
            _final_content = _extracted_text
            if _image_markers:
                _final_content += "\n" + "\n".join(_image_markers)
            # Rewrite as text message so existing handler processes it
            message["content"] = _json_post.dumps({"text": _final_content})
            msg_type = "text"
            logger.info(f"[Feishu] Normalized post → text='{_extracted_text[:100]}', images={len(_image_markers)}")

        if msg_type in ("file", "image"):
            _schedule_background(
                _handle_feishu_file(
                    agent_id,
                    config,
                    message,
                    sender_open_id,
                    sender_user_id_from_event,
                    chat_type,
                    chat_id,
                )
            )
            return {"code": 0, "msg": "ok"}

        if msg_type == "text":
            import json
            import re

            content = json_loads_object(json_as_str_or(message.get("content"), "{}"))
            user_text = json_as_str_or(content.get("text"), "")

            # Strip @mention tags (e.g. @_user_1) from group messages
            user_text = re.sub(r"@_user_\d+", "", user_text).strip()

            if not user_text:
                return {"code": 0, "msg": "empty message after stripping mentions"}

            # Detect task creation intent
            task_match = re.search(
                r"(?:创建|新建|添加|建一个|帮我建)(?:一个)?(?:任务|待办|todo)[，,：:\s]*(.+)", user_text, re.IGNORECASE
            )

            # Determine conversation_id for history isolation
            # Group chats: use chat_id; P2P chats: prefer user_id (tenant-stable)
            if chat_type == "group" and chat_id:
                conv_id = f"feishu_group_{chat_id}"
            else:
                conv_id = f"feishu_p2p_{sender_user_id_from_event or sender_open_id}"

            agent_obj = await channel_inbound.load_agent(agent_id)
            if agent_obj is None:
                logger.warning(f"[Feishu] Agent {agent_id} not found")
                return {"code": 1, "msg": "Agent not found"}
            creator_id = agent_obj.creator_id if agent_obj else agent_id

            # --- Resolve Feishu sender identity & find/create platform user ---
            sender_name = ""
            sender_user_id_feishu = sender_user_id_from_event  # tenant-level user_id, pre-filled from event body
            extra_info: JsonObject = {
                "open_id": sender_open_id,
                "external_id": sender_user_id_feishu or None,
            }

            try:
                async with httpx.AsyncClient() as _client:
                    typed_client: httpx.AsyncClient = _client
                    _tok_resp: httpx.Response = await typed_client.post(
                        "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
                        json={"app_id": app_id, "app_secret": app_secret},
                    )
                    _app_token = _app_access_token(_tok_resp)
                    if _app_token:
                        _user_resp: httpx.Response = await typed_client.get(
                            f"https://open.feishu.cn/open-apis/contact/v3/users/{sender_open_id}",
                            params={"user_id_type": "open_id"},
                            headers={"Authorization": f"Bearer {_app_token}"},
                        )
                        _user_data = _response_json_object(_user_resp)
                        logger.info(
                            f"[Feishu] Sender resolve: code={_user_data.get('code')}, msg={_user_data.get('msg', '')}"
                        )
                        if _user_data.get("code") == 0:
                            _user_info = _json_object(_json_object(_user_data.get("data")).get("user"))
                            sender_name = json_as_str_or(_user_info.get("name"), "")
                            sender_user_id_feishu = json_as_str_or(_user_info.get("user_id"), "")
                            sender_email = json_as_str_or(_user_info.get("email"), "") or json_as_str_or(
                                _user_info.get("enterprise_email"), ""
                            )
                            # Feishu contact API returns 'avatar' as a dict
                            # (keys: avatar_240, avatar_640, avatar_origin), NOT a plain URL.
                            # We must extract a string to avoid a DataError when writing to the DB.
                            _avatar_url = _avatar_url_from_user(_user_info)
                            extra_info = {
                                "name": sender_name,
                                "email": sender_email,
                                "mobile": _user_info.get("mobile"),
                                "avatar_url": _avatar_url,
                                "external_id": _user_info.get("user_id"),
                                "unionid": _user_info.get("union_id"),
                                "open_id": sender_open_id,
                            }
                            logger.info(f"[Feishu] Resolved sender: {sender_name} (user_id={sender_user_id_feishu})")
                            # Cache sender info so feishu_user_search can find them by name
                            if sender_name and sender_open_id:
                                try:
                                    import json as _cj
                                    import pathlib as _pl
                                    import time as _ct

                                    _safe_id = str(agent_id).replace("..", "").replace("/", "")
                                    _cache = _pl.Path(f"/data/workspaces/{_safe_id}/feishu_contacts_cache.json")
                                    await asyncio.to_thread(_cache.parent.mkdir, parents=True, exist_ok=True)
                                    _existing: JsonObject = {}
                                    if await asyncio.to_thread(_cache.exists):
                                        try:
                                            _existing = json_loads_object(await asyncio.to_thread(_cache.read_text))
                                        except (OSError, _cj.JSONDecodeError) as _cache_error:
                                            logger.warning(f"[Feishu] Contact cache read failed: {_cache_error}")
                                    # Key by user_id when available (tenant-stable), fallback to open_id
                                    _users: dict[str, JsonObject] = {}
                                    for _u in object_list_from_row(_existing.get("users")):
                                        user_obj = _json_object(_u)
                                        _key = json_as_str_or(user_obj.get("user_id") or user_obj.get("open_id"), "")
                                        _users[_key] = user_obj
                                    _cache_key = sender_user_id_feishu or sender_open_id
                                    _users[_cache_key] = {
                                        "open_id": sender_open_id,
                                        "name": sender_name,
                                        "email": sender_email,
                                        "user_id": sender_user_id_feishu,
                                    }
                                    _ = await asyncio.to_thread(
                                        _cache.write_text,
                                        _cj.dumps(
                                            {"ts": _ct.time(), "users": list(_users.values())}, ensure_ascii=False
                                        ),
                                        encoding="utf-8",
                                    )
                                    import os as _os

                                    await asyncio.to_thread(_os.chmod, str(_cache), 0o600)
                                except Exception as _ce:
                                    logger.error(f"[Feishu] Cache write failed: {_ce}")
            except Exception as e:
                logger.error(f"[Feishu] Failed to resolve sender: {e}")

            # Resolve channel user via unified service (uses OrgMember + SSO patterns)
            from app.services.channel_user_service import channel_user_service

            try:
                platform_user = await channel_user_service.resolve_channel_user(
                    db=None,
                    agent=agent_obj,
                    channel_type="feishu",
                    # For Feishu, external_user_id is strictly user_id (tenant-stable).
                    external_user_id=sender_user_id_feishu or None,
                    extra_info=extra_info,
                )
            except Exception as e:
                from app.services.channel_user_service import ChannelUserResolutionError

                if isinstance(e, ChannelUserResolutionError):
                    logger.warning(f"[Feishu] Sender resolution refused: {e}")
                    _reply_to = chat_id if chat_type == "group" else sender_open_id
                    _rid_type = "chat_id" if chat_type == "group" else "open_id"
                    _ = await feishu_service.send_message(
                        app_id,
                        app_secret,
                        _reply_to,
                        "text",
                        json.dumps({"text": _USER_RESOLUTION_ERROR_TIP}),
                        receive_id_type=_rid_type,
                    )
                    return {"code": 0, "msg": "user_resolution_skipped"}
                raise
            platform_user_id = platform_user.id

            # ── Shared inbound: session → history → user message ──
            _is_group = chat_type == "group"
            _sess = await channel_inbound.open_channel_session(
                agent_id=agent_id,
                user_id=platform_user_id if not _is_group else creator_id,
                external_conv_id=conv_id,
                source_channel="feishu",
                first_message_title=user_text,
                is_group=_is_group,
                group_name=f"Feishu Group {chat_id[:8]}" if _is_group else None,
            )
            session_conv_id = str(_sess.id)
            history = await channel_inbound.load_history_for_session(
                agent_id=agent_id,
                session=_sess,
                context_window_size=agent_obj.context_window_size,
            )
            await channel_inbound.persist_user_message(
                agent_id=agent_id,
                user_id=platform_user_id,
                session=_sess,
                content=user_text,
                agent=agent_obj,
            )

            # Prepend sender identity so the agent knows who is talking
            llm_user_text = user_text
            if sender_name:
                llm_user_text = f"[发送者: {sender_name}] {user_text}"

            # ── Inject recent uploaded file context ──────────────────────────
            # Check the uploads directory for recently modified files (within 30 min).
            # This is more reliable than scanning DB history, because the file save
            # to disk always succeeds even if the DB transaction fails.
            try:
                import time as _time

                _storage = get_storage_backend()
                _upload_key = agent_upload_key(agent_id, "placeholder").rsplit("/", 1)[0]
                _recent_file_path = None
                if "uploads/" not in user_text and "workspace/" not in user_text:
                    _now = _time.time()
                    if await _storage.exists(_upload_key) and await _storage.is_dir(_upload_key):
                        _candidates = sorted(
                            [e for e in await _storage.list_dir(_upload_key) if not e.is_dir],
                            key=_storage_mtime,
                            reverse=True,
                        )
                        for _entry in _candidates:
                            _mtime = _storage_mtime(_entry)
                            if _mtime and (_now - _mtime) < 1800:
                                _recent_file_path = f"uploads/{_entry.name}"
                                break
                if _recent_file_path:
                    # _recent_file_path is relative to uploads dir; agent workspace root is
                    # AGENT_DATA_DIR/{agent_id}/, so the correct relative path is workspace/uploads/
                    _ws_rel_path = f"workspace/{_recent_file_path}"
                    llm_user_text = (
                        llm_user_text
                        + f"\n\n[System notice: The user just uploaded a file to the workspace at `{_ws_rel_path}`. "
                        + "If the user's instruction refers to this article, file, or document, "
                        + f'call read_document(path="{_ws_rel_path}") immediately to read it. Do not use list_files to verify it first; read it directly.]'
                    )
                    logger.info(f"[Feishu] Injected recent file hint: {_ws_rel_path}")
            except Exception as _fe:
                logger.error(f"[Feishu] File injection error: {_fe}")

            # Set sender open_id contextvar so calendar tool can auto-invite the requester
            from app.services.agent_tool_exec.channel_context import channel_feishu_sender_open_id as _cfso

            _cfso_token = _cfso.set(sender_open_id)

            # Set channel_file_sender contextvar so the agent can send files back via Feishu
            from app.services.agent_tool_exec.channel_context import channel_file_sender as _cfs

            _reply_to_id = chat_id if chat_type == "group" else sender_open_id
            _rid_type = "chat_id" if chat_type == "group" else "open_id"

            async def _feishu_file_sender(file_path: Path, msg: str = "") -> None:
                try:
                    _ = await feishu_service.upload_and_send_file(
                        app_id,
                        app_secret,
                        _reply_to_id,
                        file_path,
                        receive_id_type=_rid_type,
                        accompany_msg=msg,
                    )
                except Exception as _upload_err:
                    # Fallback: send a download link when upload permission is not granted
                    import pathlib as _pathlib

                    from app.config import get_settings as _gs_fallback

                    _fs = _gs_fallback()
                    _base_url_raw: object = getattr(_fs, "BASE_URL", "")
                    _base_url = json_as_str_or(_base_url_raw, "").rstrip("/")
                    _fp = _pathlib.Path(file_path)
                    _parts = list(_fp.parts)
                    try:
                        _workspace_idx = _parts.index("workspace")
                        _rel = "/".join(_parts[_workspace_idx:])
                    except ValueError:
                        _ws_root = _pathlib.Path(_fs.STORAGE_LOCAL_ROOT or _fs.AGENT_DATA_DIR)
                        try:
                            _rel = str(_fp.relative_to(_ws_root / str(agent_id)))
                        except ValueError:
                            _rel = _fp.name
                    _fallback_parts = []
                    if msg:
                        _fallback_parts.append(msg)
                    if _base_url:
                        _dl_url = f"{_base_url}/api/agents/{agent_id}/files/download?path={_rel}"
                        _fallback_parts.append(f"📎 {_fp.name}\n🔗 {_dl_url}")
                    _fallback_parts.append(
                        f"⚠️ Direct file delivery failed ({_upload_err})\n"
                        + "To let the agent send Feishu files directly, enable the "
                        + "`im:resource` (that is, `im:resource:upload`) permission for the app in the Feishu Open Platform and publish the version."
                    )
                    _ = await feishu_service.send_message(
                        app_id,
                        app_secret,
                        _reply_to_id,
                        "text",
                        _json.dumps({"text": "\n\n".join(_fallback_parts)}),
                        receive_id_type=_rid_type,
                    )

            _cfs_token = _cfs.set(_feishu_file_sender)

            _reply_target = chat_id if chat_type == "group" and chat_id else sender_open_id
            _rid_type = "chat_id" if chat_type == "group" and chat_id else "open_id"

            _stream_buffer: list[str] = []
            _thinking_buffer: list[str] = []
            _agent_name = agent_obj.name if agent_obj else "AI Reply"
            _tool_errors: list[str] = []
            _tool_status_running: dict[str, str] = {}
            _tool_status_done: list[str] = []
            _patch_queue = _SerialPatchQueue()
            _heartbeat_task: asyncio.Task[None] | None = None
            _llm_done = False
            _last_flushed_hash: int = 0
            _last_flush_time = 0.0
            _flush_interval = 1.0
            _patch_msg_id: str | None = None
            _flush_lock = asyncio.Lock()

            def _visible_tool_status_lines() -> list[str]:
                done_visible = _tool_status_done[-_TOOL_STATUS_KEEP_LINES:]
                running_visible = list(_tool_status_running.values())
                return done_visible + running_visible

            async def _queue_patch_card(card: JsonObject, stage: str) -> None:
                message_id = _patch_msg_id
                if message_id is None:
                    return
                payload = _json.dumps(card)

                async def _job() -> None:
                    try:
                        _ = await feishu_service.patch_message(
                            app_id,
                            app_secret,
                            message_id,
                            payload,
                            stage=stage,
                        )
                    except Exception as e:
                        logger.warning(f"[Feishu] Patch failed (stage={stage}, message_id={message_id}): {e}")

                _patch_queue.enqueue(_job)

            _init_card = _build_card(
                answer_text="",
                streaming=True,
                agent_name=_agent_name,
            )
            try:
                _init_resp = await feishu_service.send_message(
                    app_id,
                    app_secret,
                    _reply_target,
                    "interactive",
                    _json.dumps(_init_card),
                    receive_id_type=_rid_type,
                    stage="stream_init_card",
                )
                _patch_msg_id = _message_id_from_send_response(_init_resp)
            except Exception as e:
                logger.error(f"[Feishu] Failed to send init streaming card: {e}")

            async def _flush_stream(reason: str, force: bool = False) -> None:
                nonlocal _last_flushed_hash, _last_flush_time
                if not _patch_msg_id:
                    return
                async with _flush_lock:
                    now = time.time()
                    if not force and now - _last_flush_time < _flush_interval:
                        return
                    accumulated = "".join(_stream_buffer)
                    thinking_text = "".join(_thinking_buffer)
                    tool_status_lines = _visible_tool_status_lines()
                    current_hash = hash(accumulated + thinking_text + "\n".join(tool_status_lines))
                    if reason == "heartbeat" and current_hash == _last_flushed_hash:
                        return
                    _last_flushed_hash = current_hash
                    card = _build_card(
                        answer_text=accumulated,
                        thinking_text=thinking_text,
                        streaming=True,
                        tool_status_lines=tool_status_lines,
                        agent_name=_agent_name,
                    )
                    await _queue_patch_card(card, stage=f"stream_{reason}")
                    _last_flush_time = now

            async def _ws_on_chunk(text: str) -> None:
                _stream_buffer.append(text)
                if _patch_msg_id:
                    await _flush_stream("chunk")

            async def _ws_on_thinking(text: str) -> None:
                _thinking_buffer.append(text)
                if _patch_msg_id:
                    await _flush_stream("thinking")

            async def _ws_on_tool_call(evt: ToolCallbackData) -> None:
                tool_name = evt.get("name") or "unknown_tool"
                call_id = evt.get("call_id") or tool_name
                status = (evt.get("status") or "").lower()
                result = evt.get("result")
                if status == "running":
                    _tool_status_running[call_id] = f"⏳ Tool running: `{tool_name}`"
                elif status == "done":
                    _ = _tool_status_running.pop(call_id, None)
                    normalized_error = _normalize_tool_error(tool_name, result)
                    if normalized_error:
                        _tool_errors.append(normalized_error)
                        _tool_status_done.append(f"❌ Tool failed: `{tool_name}`")
                    else:
                        _tool_status_done.append(f"✅ Tool done: `{tool_name}`")
                elif status and status not in {"running", "done"}:
                    _ = _tool_status_running.pop(call_id, None)
                    _tool_errors.append(f"`{tool_name}`: tool status `{status}`")
                    _tool_status_done.append(f"Info: Tool update: `{tool_name}` ({status})")

                if status and status != "running":
                    raw_args: object = evt.get("args") or evt.get("arguments") or {}
                    tool_arguments = _json_object(raw_args)
                    await _save_feishu_tool_call(
                        agent_id=agent_id,
                        user_id=platform_user_id,
                        conversation_id=session_conv_id,
                        tool_name=tool_name,
                        status=status,
                        arguments=tool_arguments,
                        result=(str(result) if result is not None else "")[:500],
                        tool_call_id=evt.get("call_id"),
                        reasoning_content=evt.get("reasoning_content"),
                    )
                if _patch_msg_id:
                    await _flush_stream("tool", force=True)

            async def _heartbeat() -> None:
                while not _llm_done:
                    await asyncio.sleep(_flush_interval)
                    if _patch_msg_id:
                        await _flush_stream("heartbeat")

            if _patch_msg_id:
                _heartbeat_task = asyncio.create_task(_heartbeat())

            # Call LLM via shared channel inbound helper (streaming callbacks preserved)
            try:
                reply_text = await channel_inbound.generate_channel_reply(
                    agent_id=agent_id,
                    user_text=llm_user_text,
                    history=history,
                    user_id=platform_user_id,
                    session_id=session_conv_id,
                    agent_model=_agent_model,
                    llm_model=_llm_model,
                    fallback_model=_fallback_model,
                    on_chunk=_ws_on_chunk,
                    on_thinking=_ws_on_thinking,
                    on_tool_call=_ws_on_tool_call,
                )
            finally:
                _llm_done = True
                if _heartbeat_task:
                    _ = _heartbeat_task.cancel()
                    try:
                        await _heartbeat_task
                    except asyncio.CancelledError:
                        logger.debug("[Feishu] Text heartbeat cancelled")
                    except Exception as error:
                        logger.debug(f"[Feishu] Text heartbeat shutdown failed: {error}")
                _cfs.reset(_cfs_token)
                _cfso.reset(_cfso_token)
            logger.info(f"[Feishu] LLM reply: {reply_text[:100]}")

            # If task creation detected, create a real Task record
            if task_match:
                task_title = task_match.group(1).strip()
                if task_title:
                    try:
                        from app.services.task_executor import execute_task

                        _task_agent = await agent_dao.get(agent_id)
                        _task_creator_id = _task_agent.creator_id if _task_agent else agent_id
                        task_obj = await task_dao.create(
                            obj_in={
                                "agent_id": agent_id,
                                "title": task_title,
                                "created_by": _task_creator_id,
                                "status": "pending",
                                "priority": "medium",
                            }
                        )
                        _schedule_background(execute_task(task_obj.id, agent_id))
                        reply_text += f"\n\n📋 A task has been created on the task board: 【{task_title}】"
                        logger.info(f"[Feishu] Created task: {task_title}")
                    except Exception as e:
                        logger.error(f"[Feishu] Failed to create task: {e}")
                        reply_text += (
                            f"\n\n⚠️ A task was recognized, but writing it to the task board failed: {str(e)[:150]}"
                        )

            final_reply_text = _append_error_details(reply_text, _tool_errors)
            final_card = _build_card(
                answer_text=final_reply_text or "...",
                thinking_text="",
                streaming=False,
                tool_status_lines=_visible_tool_status_lines(),
                agent_name=_agent_name,
            )

            if _patch_msg_id:
                try:
                    await _patch_queue.drain()
                except Exception as e:
                    logger.warning(f"[Feishu] Drain patch queue failed before final patch: {e}")
                try:
                    _ = await feishu_service.patch_message(
                        app_id,
                        app_secret,
                        _patch_msg_id,
                        _json.dumps(final_card),
                        stage="stream_final",
                    )
                except Exception as e:
                    logger.error(f"[Feishu] Failed to patch final interactive reply: {e}")
                    try:
                        _ = await feishu_service.send_message(
                            app_id,
                            app_secret,
                            _reply_target,
                            "text",
                            _json.dumps({"text": final_reply_text}),
                            receive_id_type=_rid_type,
                            stage="final_after_task_fallback_text",
                        )
                    except Exception as e2:
                        logger.error(f"[Feishu] Failed to send fallback text reply: {e2}")
            else:
                try:
                    _ = await feishu_service.send_message(
                        app_id,
                        app_secret,
                        _reply_target,
                        "interactive",
                        _json.dumps(final_card),
                        receive_id_type=_rid_type,
                        stage="final_after_task",
                    )
                except Exception as e:
                    logger.error(f"[Feishu] Failed to send final interactive reply: {e}")
                    try:
                        _ = await feishu_service.send_message(
                            app_id,
                            app_secret,
                            _reply_target,
                            "text",
                            _json.dumps({"text": final_reply_text}),
                            receive_id_type=_rid_type,
                            stage="final_after_task_fallback_text",
                        )
                    except Exception as e2:
                        logger.error(f"[Feishu] Failed to send fallback text reply: {e2}")

            # Log activity
            from app.services.activity_logger import log_activity

            await log_activity(
                agent_id,
                "chat_reply",
                f"Replied to Feishu message: {final_reply_text[:80]}",
                detail={"channel": "feishu", "user_text": user_text[:200], "reply": final_reply_text[:500]},
            )

            # Save assistant reply to history (shared inbound persist)
            await channel_inbound.persist_assistant_message(
                agent_id=agent_id,
                user_id=platform_user_id,
                session=_sess,
                content=final_reply_text,
                thinking="".join(_thinking_buffer) or None,
                agent=agent_obj,
                touch_last_active=True,
            )

    if event_id:
        await channel_dedup.mark_processed_shared("feishu", event_id, cap=2000)
    return {"code": 0, "msg": "ok"}


IMPORT_RE = None  # lazy sentinel
_FILE_ACK_MESSAGES = [
    "I received your file. How can I help?",
    "Your file is here. How would you like me to handle it?",
    "I received the file. Please tell me what you need.",
    "File received. I am ready to help you process it.",
    "Got it. What would you like me to do with this file?",
]


async def _handle_feishu_file(
    agent_id: uuid.UUID,
    config: ChannelConfigRecord,
    message: JsonObject,
    sender_open_id: str,
    sender_user_id_from_event: str,
    chat_type: str,
    chat_id: str,
) -> None:
    """Handle incoming file or image messages from Feishu (runs as a background task)."""
    import asyncio
    import json
    import random

    app_id = config.app_id or ""
    app_secret = config.app_secret or ""

    msg_type = json_as_str_or(message.get("message_type"), "file")
    message_id = json_as_str_or(message.get("message_id"), "")
    content = json_loads_object(json_as_str_or(message.get("content"), "{}"))

    # Extract file key and name
    if msg_type == "image":
        file_key = json_as_str_or(content.get("image_key"), "")
        filename = f"image_{file_key[-8:]}.jpg" if file_key else "image.jpg"
        res_type = "image"
    else:
        file_key = json_as_str_or(content.get("file_key"), "")
        filename = json_as_str_or(content.get("file_name"), "") or f"file_{file_key[-8:]}.bin"
        res_type = "file"

    if not file_key:
        logger.warning(f"[Feishu] No file_key in {msg_type} message")
        return

    # Resolve workspace upload dir
    # Download the file
    try:
        file_bytes = await feishu_service.download_message_resource(
            app_id, app_secret, message_id, file_key, res_type
        )
        _, workspace_path, _save_path = await store_agent_upload(
            agent_id,
            filename,
            file_bytes,
            content_type="image/jpeg" if msg_type == "image" else None,
        )
        logger.info(f"[Feishu] Saved {msg_type} to {workspace_path} ({len(file_bytes)} bytes)")
    except Exception as e:
        logger.error(f"[Feishu] Failed to download {msg_type}: {e}")
        err_tip = "Sorry, the file download failed. The bot may be missing the `im:resource` permission for reading files.\nIn the Feishu Open Platform, go to Permission Management -> Import permissions JSON in bulk -> republish the bot version, then try again."
        try:
            import json as _j

            if chat_type == "group" and chat_id:
                _ = await feishu_service.send_message(
                    app_id,
                    app_secret,
                    chat_id,
                    "text",
                    _j.dumps({"text": err_tip}),
                    receive_id_type="chat_id",
                )
            else:
                _ = await feishu_service.send_message(
                    app_id, app_secret, sender_open_id, "text", _j.dumps({"text": err_tip})
                )
        except Exception as e2:
            logger.error(f"[Feishu] Also failed to send error tip: {e2}")
        return

    # Resolve platform user and session (shared inbound helpers)
    agent_obj = await channel_inbound.load_agent(agent_id)
    if agent_obj is None:
        logger.warning(f"[Feishu] Agent {agent_id} not found for file event")
        return

    # Resolve sender's Feishu user_id (more stable than open_id)
    sender_user_id_feishu = sender_user_id_from_event or ""
    extra_info: JsonObject = {
        "open_id": sender_open_id,
        "external_id": sender_user_id_feishu or None,
    }
    try:
        async with httpx.AsyncClient() as _fc:
            typed_client: httpx.AsyncClient = _fc
            _tr: httpx.Response = await typed_client.post(
                "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            )
            _at = _app_access_token(_tr)
            if _at:
                _ur: httpx.Response = await typed_client.get(
                    f"https://open.feishu.cn/open-apis/contact/v3/users/{sender_open_id}",
                    params={"user_id_type": "open_id"},
                    headers={"Authorization": f"Bearer {_at}"},
                )
                _ud = _response_json_object(_ur)
                if _ud.get("code") == 0:
                    _user_info = _json_object(_json_object(_ud.get("data")).get("user"))
                    sender_user_id_feishu = json_as_str_or(_user_info.get("user_id"), "")
                    extra_info = {
                        "name": _user_info.get("name"),
                        "avatar_url": _avatar_url_from_user(_user_info),
                        "email": _user_info.get("email"),
                        "mobile": _user_info.get("mobile"),
                        "external_id": _user_info.get("user_id"),
                        "unionid": _user_info.get("union_id"),
                        "open_id": sender_open_id,
                    }
    except Exception as error:
        logger.warning(f"[Feishu] Failed to resolve file sender details: {error}")

    # Resolve channel user via unified service (uses OrgMember + SSO patterns)
    from app.services.channel_user_service import channel_user_service

    try:
        platform_user = await channel_user_service.resolve_channel_user(
            db=None,
            agent=agent_obj,
            channel_type="feishu",
            # For Feishu, external_user_id is strictly user_id (tenant-stable).
            external_user_id=sender_user_id_feishu or None,
            extra_info=extra_info,
        )
    except Exception as e:
        from app.services.channel_user_service import ChannelUserResolutionError

        if isinstance(e, ChannelUserResolutionError):
            logger.warning(f"[Feishu] File sender resolution refused: {e}")
            _reply_to = chat_id if chat_type == "group" else sender_open_id
            _rid_type = "chat_id" if chat_type == "group" else "open_id"
            _ = await feishu_service.send_message(
                app_id,
                app_secret,
                _reply_to,
                "text",
                json.dumps({"text": _USER_RESOLUTION_ERROR_TIP}),
                receive_id_type=_rid_type,
            )
            return
        raise
    platform_user_id = platform_user.id

    # Conv ID - prefer user_id for session continuity
    if chat_type == "group" and chat_id:
        conv_id = f"feishu_group_{chat_id}"
    else:
        conv_id = f"feishu_p2p_{sender_user_id_feishu or sender_open_id}"

    # Shared inbound: session → history → user message
    _is_group_file = chat_type == "group"
    # For group file sessions, use agent creator as placeholder user_id
    _file_user_id = agent_obj.creator_id if _is_group_file else platform_user_id
    _sess = await channel_inbound.open_channel_session(
        agent_id=agent_id,
        user_id=_file_user_id,
        external_conv_id=conv_id,
        source_channel="feishu",
        first_message_title=f"[文件] {filename}",
        is_group=_is_group_file,
        group_name=f"Feishu Group {chat_id[:8]}" if _is_group_file else None,
    )
    session_conv_id = str(_sess.id)

    # Store user message - include base64 marker for images so LLM can see them
    if msg_type == "image":
        import base64 as _b64_img

        _b64_data = _b64_img.b64encode(file_bytes).decode("ascii")
        _image_marker = f"[image_data:data:image/jpeg;base64,{_b64_data}]"
        user_msg_content = f"[用户发送了图片]\n{_image_marker}"
    else:
        user_msg_content = f"[file:{filename}]"

    _history = await channel_inbound.load_history_for_session(
        agent_id=agent_id,
        session=_sess,
        context_window_size=agent_obj.context_window_size,
    )
    await channel_inbound.persist_user_message(
        agent_id=agent_id,
        user_id=platform_user_id,
        session=_sess,
        content=user_msg_content if msg_type != "image" else f"[file:{filename}]",
        agent=agent_obj,
    )

    # Pre-load agent/model for LLM call
    _agent_model_img, _llm_model_img, _fallback_model_img = await _load_agent_and_model(None, agent_id)

    # For images: call LLM so vision models can actually see the image
    if msg_type == "image":
        import json as _json_card_img

        # Send initial loading card
        _reply_to = chat_id if chat_type == "group" else sender_open_id
        _rid_type = "chat_id" if chat_type == "group" else "open_id"
        _agent_name = agent_obj.name if agent_obj else "AI"
        _init_card = {
            "config": {"update_multi": True},
            "header": {"template": "blue", "title": {"content": "Recognizing image...", "tag": "plain_text"}},
            "elements": [{"tag": "markdown", "content": "..."}],
        }
        _patch_msg_id = None
        try:
            _init_resp = await feishu_service.send_message(
                app_id,
                app_secret,
                _reply_to,
                "interactive",
                _json_card_img.dumps(_init_card),
                receive_id_type=_rid_type,
                stage="image_stream_init_card",
            )
            _patch_msg_id = _message_id_from_send_response(_init_resp)
        except Exception as _e_init:
            logger.error(f"[Feishu] Failed to send init card for image: {_e_init}")

        _img_stream_buf: list[str] = []
        _img_last_flush = time.time()
        _img_flush_interval = 1.0
        _img_patch_queue = _SerialPatchQueue()
        _img_heartbeat_task: asyncio.Task[None] | None = None
        _img_llm_done = False
        _img_last_flushed_hash: int = 0  # Content hash to skip no-op heartbeat patches

        async def _queue_image_patch(_card: JsonObject, _stage: str) -> None:
            """Enqueue a serialized PATCH request for the image streaming card."""
            message_id = _patch_msg_id
            if message_id is None:
                return
            _payload = _json_card_img.dumps(_card)

            async def _job() -> None:
                try:
                    _ = await feishu_service.patch_message(
                        app_id,
                        app_secret,
                        message_id,
                        _payload,
                        stage=_stage,
                    )
                except Exception as _e_patch:
                    logger.warning(f"[Feishu] Image patch failed (stage={_stage}, message_id={message_id}): {_e_patch}")

            _img_patch_queue.enqueue(_job)

        async def _flush_image_stream(reason: str, force: bool = False) -> None:
            """Build and enqueue an image streaming card update.

            Reuses _build_card so the image path supports the same thinking
            and tool-status sections as the text streaming path.
            Skips the patch on heartbeat ticks when content has not changed.
            """
            nonlocal _img_last_flush, _img_last_flushed_hash
            now = time.time()
            if not force and now - _img_last_flush < _img_flush_interval:
                return
            # Reuse the shared card builder (no tool_status for image path yet,
            # but the builder is ready to accept them in the future).
            _card = _build_card(
                "".join(_img_stream_buf),
                streaming=True,
                agent_name=_agent_name,
            )
            # Skip no-op heartbeat patches when content hasn't changed.
            current_hash = hash("".join(_img_stream_buf))
            if reason == "heartbeat" and current_hash == _img_last_flushed_hash:
                return
            _img_last_flushed_hash = current_hash
            await _queue_image_patch(_card, _stage=f"image_stream_{reason}")
            _img_last_flush = now

        async def _img_on_chunk(text: str) -> None:
            _img_stream_buf.append(text)
            if _patch_msg_id:
                await _flush_image_stream("chunk")

        async def _img_heartbeat() -> None:
            while not _img_llm_done:
                await asyncio.sleep(_img_flush_interval)
                if _patch_msg_id:
                    await _flush_image_stream("heartbeat")

        if _patch_msg_id:
            _img_heartbeat_task = asyncio.create_task(_img_heartbeat())

        # Call LLM with image marker - vision models will parse it
        try:
            reply_text = await channel_inbound.generate_channel_reply(
                agent_id=agent_id,
                user_text=user_msg_content,
                history=_history,
                user_id=platform_user_id,
                session_id=session_conv_id,
                agent_model=_agent_model_img,
                llm_model=_llm_model_img,
                fallback_model=_fallback_model_img,
                on_chunk=_img_on_chunk,
            )
        finally:
            _img_llm_done = True
            if _img_heartbeat_task:
                _ = _img_heartbeat_task.cancel()
                try:
                    await _img_heartbeat_task
                except asyncio.CancelledError:
                    logger.debug("[Feishu] Image heartbeat cancelled")
                except Exception as error:
                    logger.debug(f"[Feishu] Image heartbeat shutdown failed: {error}")

        logger.info(f"[Feishu] Image LLM reply: {reply_text[:100]}")

        # Send final card or fallback text
        if _patch_msg_id:
            try:
                await _img_patch_queue.drain()
            except Exception as _e_drain:
                logger.warning(f"[Feishu] Image patch queue drain failed: {_e_drain}")
            # Build final card via shared builder (consistent with text streaming path).
            _final_card = _build_card(
                reply_text or "...",
                streaming=False,
                agent_name=_agent_name,
            )
            _ = await feishu_service.patch_message(
                app_id,
                app_secret,
                _patch_msg_id,
                _json_card_img.dumps(_final_card),
                stage="image_stream_final",
            )
        else:
            try:
                _ = await feishu_service.send_message(
                    app_id,
                    app_secret,
                    _reply_to,
                    "text",
                    json.dumps({"text": reply_text}),
                    receive_id_type=_rid_type,
                    stage="image_stream_fallback_text",
                )
            except Exception as _e_fb:
                logger.error(f"[Feishu] Failed to send image reply: {_e_fb}")

        # Save assistant reply in DB
        await channel_inbound.persist_assistant_message(
            agent_id=agent_id,
            user_id=platform_user_id,
            session=_sess,
            content=reply_text,
            agent=agent_obj,
            touch_last_active=True,
        )

        # Log activity
        from app.services.activity_logger import log_activity

        await log_activity(
            agent_id,
            "chat_reply",
            f"Replied to Feishu image message: {reply_text[:80]}",
            detail={"channel": "feishu", "type": "image"},
        )
        return

    # For non-image files: send simple ack as before
    random_source = random.SystemRandom()
    await asyncio.sleep(random_source.uniform(1.0, 2.0))

    ack = random_source.choice(_FILE_ACK_MESSAGES)
    try:
        if chat_type == "group" and chat_id:
            _ = await feishu_service.send_message(
                app_id,
                app_secret,
                chat_id,
                "text",
                json.dumps({"text": ack}),
                receive_id_type="chat_id",
            )
        else:
            _ = await feishu_service.send_message(
                app_id,
                app_secret,
                sender_open_id,
                "text",
                json.dumps({"text": ack}),
            )
    except Exception as e:
        logger.error(f"[Feishu] Failed to send ack: {e}")

    # Store ack in DB
    await channel_inbound.persist_assistant_message(
        agent_id=agent_id,
        user_id=platform_user_id,
        session=_sess,
        content=ack,
        agent=agent_obj,
        touch_last_active=True,
    )


async def _download_post_images(
    agent_id: uuid.UUID,
    config: ChannelConfigRecord,
    message_id: str,
    image_keys: list[str],
) -> None:
    """Download images embedded in a Feishu post message to the agent's workspace."""
    app_id = config.app_id or ""
    app_secret = config.app_secret or ""
    for ik in image_keys:
        try:
            file_bytes = await feishu_service.download_message_resource(
                app_id, app_secret, message_id, ik, "image"
            )
            _, workspace_path, _ = await store_agent_upload(
                agent_id,
                f"image_{ik[-8:]}.jpg",
                file_bytes,
                content_type="image/jpeg",
            )
            logger.info(f"[Feishu] Saved post image to {workspace_path} ({len(file_bytes)} bytes)")
        except Exception as e:
            logger.error(f"[Feishu] Failed to download post image {ik}: {e}")


async def _load_agent_and_model(
    db: object | None, agent_id: uuid.UUID
) -> tuple[AgentRecord | None, LLMModelRecord | None, LLMModelRecord | None]:
    """Load agent and LLM model configs.

    Returns (agent, model, fallback_model). ``db`` is accepted for call-site
    compatibility and ignored (pure-psycopg path).
    """
    del db
    from app.dao import agent_dao, llm_model_dao

    agent = await agent_dao.get(agent_id)
    if not agent:
        return None, None, None

    model_ids = [mid for mid in (agent.primary_model_id, agent.fallback_model_id) if mid]
    loaded = {row.id: row for row in await llm_model_dao.get_many(model_ids)}
    model = loaded.get(agent.primary_model_id) if agent.primary_model_id else None
    if model and not model.enabled:
        logger.info(f"[Channel] Primary model {model.model} is disabled, skipping")
        model = None

    fallback_model = loaded.get(agent.fallback_model_id) if agent.fallback_model_id else None
    if fallback_model and not fallback_model.enabled:
        logger.info(f"[Channel] Fallback model {fallback_model.model} is disabled, skipping")
        fallback_model = None

    if not model and fallback_model:
        model = fallback_model
        fallback_model = None
        logger.warning(f"[Channel] Primary model unavailable, using fallback: {model.model}")

    return agent, model, fallback_model


async def _call_llm_with_config(
    agent: AgentRecord | None,
    model: LLMModelRecord | None,
    fallback_model: LLMModelRecord | None,
    agent_id: uuid.UUID,
    user_text: str,
    history: list[OpenAIMessage] | None = None,
    user_id: uuid.UUID | None = None,
    session_id: str = "",
    on_chunk: ChunkCallback | None = None,
    on_thinking: ThinkingCallback | None = None,
    on_tool_call: ToolCallback | None = None,
) -> str:
    """Call LLM with pre-loaded agent/model objects. No DB session needed.

    This is the hot path - all DB queries should be done before calling this.
    """
    from app.services.llm import call_llm

    if agent is None:
        return "⚠️ Agent not found."
    if is_agent_expired(agent):
        return "This Agent has expired and is off duty. Please contact your admin to extend its service."

    if not model:
        return f"⚠️ {agent.name} has no LLM model configured. Set one in the admin console."

    # Build conversation messages (without system prompt - call_llm adds it)
    messages: list[OpenAIMessage] = []
    ctx_size = agent.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE
    if history:
        messages.extend(truncate_messages_with_pair_integrity(history, ctx_size))
    messages.append({"role": "user", "content": user_text})

    effective_user_id = user_id or agent_id
    _timeout = _get_llm_timeout(model)
    turn = TurnContext(agent=agent, primary_model=model, fallback_model=fallback_model)

    try:
        return await asyncio.wait_for(
            call_llm(
                model,
                messages,
                agent.name,
                agent.role_description or "",
                agent_id=agent_id,
                user_id=effective_user_id,
                session_id=session_id,
                supports_vision=model.supports_vision,
                on_chunk=on_chunk,
                on_thinking=on_thinking,
                on_tool_call=on_tool_call,
                turn=turn,
            ),
            timeout=_timeout,
        )
    except TimeoutError:
        logger.error(
            f"[LLM] Call timed out after {_timeout}s (agent_id={agent_id}, model={model.model})"
        )
        if fallback_model:
            _fb_timeout = _get_llm_timeout(fallback_model)
            logger.info(
                f"[LLM] Retrying timed-out request with fallback model: {fallback_model.model} (timeout={_fb_timeout}s)"
            )
            try:
                return await asyncio.wait_for(
                    call_llm(
                        fallback_model,
                        messages,
                        agent.name,
                        agent.role_description or "",
                        agent_id=agent_id,
                        user_id=effective_user_id,
                        session_id=session_id,
                        supports_vision=fallback_model.supports_vision,
                        on_chunk=on_chunk,
                        on_thinking=on_thinking,
                        on_tool_call=on_tool_call,
                        turn=turn,
                    ),
                    timeout=_fb_timeout,
                )
            except TimeoutError:
                logger.error(
                    f"[LLM] Fallback call also timed out after {_fb_timeout}s "
                    + f"(agent_id={agent_id}, model={fallback_model.model})"
                )
                return f"⚠️ Model response timed out (>{int(_fb_timeout)}s). Please retry or shorten your request."
            except Exception as e2:
                import traceback

                traceback.print_exc()
                return f"⚠️ Model error: Primary Timeout | Fallback: {str(e2)[:80]}"
        return f"⚠️ Model response timed out (>{int(_timeout)}s). Please retry or shorten your request."
    except Exception as e:
        import traceback

        traceback.print_exc()
        error_msg = str(e) or repr(e)
        logger.error(f"[LLM] Primary model error: {error_msg}")
        if fallback_model:
            logger.info(f"[LLM] Retrying with fallback model: {fallback_model.model}")
            _fb_timeout = _get_llm_timeout(fallback_model)
            try:
                return await asyncio.wait_for(
                    call_llm(
                        fallback_model,
                        messages,
                        agent.name,
                        agent.role_description or "",
                        agent_id=agent_id,
                        user_id=effective_user_id,
                        session_id=session_id,
                        supports_vision=fallback_model.supports_vision,
                        on_chunk=on_chunk,
                        on_thinking=on_thinking,
                        on_tool_call=on_tool_call,
                        turn=turn,
                    ),
                    timeout=_fb_timeout,
                )
            except TimeoutError:
                logger.error(
                    f"[LLM] Fallback call timed out after {_fb_timeout}s "
                    + f"(agent_id={agent_id}, model={fallback_model.model})"
                )
                return f"⚠️ Model error: Primary: {str(e)[:80]} | Fallback Timeout"
            except Exception as e2:
                traceback.print_exc()
                return f"⚠️ Model error: Primary: {str(e)[:80]} | Fallback: {str(e2)[:80]}"
        return f"⚠️ Model call failed: {error_msg[:150]}"


async def _call_agent_llm(
    db: object | None,
    agent_id: uuid.UUID,
    user_text: str,
    history: list[OpenAIMessage] | None = None,
    user_id: uuid.UUID | None = None,
    session_id: str = "",
    on_chunk: ChunkCallback | None = None,
    on_thinking: ThinkingCallback | None = None,
    on_tool_call: ToolCallback | None = None,
) -> str:
    """Backward-compatible wrapper: load config + call LLM in one shot.

    Prefer _load_agent_and_model + _call_llm_with_config for short-transaction
    patterns where the session should be closed before the LLM call.
    ``db`` is accepted for call-site compatibility and ignored.
    """
    agent, model, fallback_model = await _load_agent_and_model(db, agent_id)
    if not agent:
        return "⚠️ Digital employee not found"
    return await _call_llm_with_config(
        agent,
        model,
        fallback_model,
        agent_id,
        user_text,
        history=history,
        user_id=user_id,
        session_id=session_id,
        on_chunk=on_chunk,
        on_thinking=on_thinking,
        on_tool_call=on_tool_call,
    )
