"""WeChat iLink Bot long-poll manager and client helpers."""

from __future__ import annotations

import asyncio
import base64
import os
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from app.core.logging import logger
from app.dao.channel_config_dao import channel_config_dao

if TYPE_CHECKING:
    from app.services.wechat_message_processor import WeChatMessageItem

WECHAT_ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
WECHAT_CHANNEL_VERSION = "1.0.0"
WECHAT_TEXT_LIMIT = 2000
WECHAT_CONTEXT_CACHE_KEY = "recent_context_tokens"
WECHAT_CONTEXT_CACHE_LIMIT = 100


class WeChatSessionExpiredError(RuntimeError):
    """Raised when the remote iLink session has expired."""


def random_wechat_uin() -> str:
    """Generate X-WECHAT-UIN according to the protocol spec."""
    value = int.from_bytes(os.urandom(4), "big", signed=False)
    return base64.b64encode(str(value).encode("utf-8")).decode("utf-8")


def build_wechat_headers(token: str, route_tag: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token}",
        "X-WECHAT-UIN": random_wechat_uin(),
    }
    if route_tag:
        headers["SKRouteTag"] = route_tag
    return headers


def split_wechat_text(text: str, limit: int = WECHAT_TEXT_LIMIT) -> list[str]:
    """Split text conservatively following the protocol's 2000-char guidance."""
    remaining = text or ""
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        segment = remaining[:limit]
        cut = max(segment.rfind("\n\n"), segment.rfind("\n"), segment.rfind(" "))
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return chunks or [""]


async def send_wechat_text_message(
    *,
    token: str,
    base_url: str,
    to_user_id: str,
    context_token: str,
    text: str,
    route_tag: str | None = None,
) -> None:
    """Send one or more WeChat iLink text messages."""
    async with httpx.AsyncClient(timeout=20) as client:
        for chunk in split_wechat_text(text):
            resp = await client.post(
                f"{base_url.rstrip('/')}/ilink/bot/sendmessage",
                headers=build_wechat_headers(token, route_tag=route_tag),
                json={
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": to_user_id,
                        "client_id": f"maraclaw-wechat:{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}",
                        "message_type": 2,
                        "message_state": 2,
                        "context_token": context_token,
                        "item_list": [
                            {
                                "type": 1,
                                "text_item": {
                                    "text": chunk,
                                },
                            }
                        ],
                    },
                    "base_info": {
                        "channel_version": WECHAT_CHANNEL_VERSION,
                    },
                },
            )
            data = resp.json()
            if resp.status_code >= 400:
                raise RuntimeError(f"WeChat sendmessage failed: {resp.text[:300]}")
            ret = data.get("ret", 0)
            errcode = data.get("errcode", 0)
            if ret not in (0, None) or errcode not in (0, None):
                raise RuntimeError(data.get("errmsg") or f"WeChat sendmessage failed: ret={ret}, errcode={errcode}")


def update_wechat_context_cache(
    extra_config: dict[str, Any] | None,
    *,
    from_user_id: str,
    context_token: str,
    conv_id: str,
) -> dict[str, Any]:
    extra = dict(extra_config or {})
    cache = dict(extra.get(WECHAT_CONTEXT_CACHE_KEY) or {})
    cache[from_user_id] = {
        "context_token": context_token,
        "conv_id": conv_id,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if len(cache) > WECHAT_CONTEXT_CACHE_LIMIT:
        ordered = sorted(
            cache.items(),
            key=lambda item: str((item[1] or {}).get("updated_at") or ""),
            reverse=True,
        )
        cache = dict(ordered[:WECHAT_CONTEXT_CACHE_LIMIT])
    extra[WECHAT_CONTEXT_CACHE_KEY] = cache
    return extra


def get_wechat_context_entry(
    extra_config: dict[str, Any] | None,
    *,
    from_user_id: str,
) -> dict[str, Any] | None:
    cache = dict((extra_config or {}).get(WECHAT_CONTEXT_CACHE_KEY) or {})
    entry = cache.get(from_user_id)
    return entry if isinstance(entry, dict) else None


async def remember_wechat_context(
    db: Any,
    *,
    agent_id: uuid.UUID,
    from_user_id: str,
    context_token: str,
    conv_id: str,
) -> None:
    del db
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="wechat")
    if not config:
        return
    new_extra = update_wechat_context_cache(
        config.extra_config,
        from_user_id=from_user_id,
        context_token=context_token,
        conv_id=conv_id,
    )
    await channel_config_dao.update(db_obj=config, obj_in={"extra_config": new_extra})


def _extract_wechat_text(item_list: list[WeChatMessageItem] | None) -> str:
    parts: list[str] = []
    for item in item_list or []:
        if item["type"] == 1:
            text_item = item.get("text_item")
            text = text_item["text"].strip() if text_item else ""
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


class WeChatPollManager:
    """Manage WeChat iLink long-poll workers per agent."""

    def __init__(self) -> None:
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._connected: dict[uuid.UUID, bool] = {}
        self._reconcile_interval_seconds = 30

    async def start_client(self, agent_id: uuid.UUID, stop_existing: bool = True) -> None:
        if stop_existing:
            await self.stop_client(agent_id)
        task = asyncio.create_task(self._run_client(agent_id), name=f"wechat-poll-{str(agent_id)[:8]}")
        self._tasks[agent_id] = task
        self._connected[agent_id] = False

    async def stop_client(self, agent_id: uuid.UUID) -> None:
        task = self._tasks.pop(agent_id, None)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._connected[agent_id] = False
        await self._set_connected(agent_id, False)

    async def start_all(self) -> None:
        logger.info("[WeChat] Poll manager started")
        while True:
            await self.reconcile_clients()
            await asyncio.sleep(self._reconcile_interval_seconds)

    async def reconcile_clients(self) -> None:
        configured_agent_ids: set[uuid.UUID] = set()
        configs = await channel_config_dao.list_configured("wechat")
        for cfg in configs:
            token = str((cfg.extra_config or {}).get("bot_token") or "").strip()
            if token:
                configured_agent_ids.add(cfg.agent_id)

        for agent_id in configured_agent_ids:
            task = self._tasks.get(agent_id)
            if task is None or task.done():
                await self.start_client(agent_id)

        for agent_id in list(self._tasks):
            if agent_id not in configured_agent_ids:
                await self.stop_client(agent_id)

    async def _run_client(self, agent_id: uuid.UUID) -> None:
        from app.services.wechat_message_processor import process_wechat_message

        retry_delay = 2
        max_retry_delay = 30
        try:
            while True:
                config = await self._load_config(agent_id)
                if not config:
                    logger.info(f"[WeChat] Channel config missing for agent {agent_id}, stopping poller")
                    return

                extra = config.extra_config or {}
                token = str(extra.get("bot_token") or "").strip()
                base_url = str(extra.get("baseurl") or WECHAT_ILINK_BASE_URL).strip()
                route_tag = str(extra.get("route_tag") or "").strip() or None
                cursor = str(extra.get("get_updates_buf") or "")

                if not token:
                    logger.info(f"[WeChat] No bot token for agent {agent_id}, stopping poller")
                    await self._set_connected(agent_id, False)
                    return

                try:
                    data = await self._fetch_updates(token=token, base_url=base_url, cursor=cursor, route_tag=route_tag)
                    self._connected[agent_id] = True
                    await self._set_connected(agent_id, True)
                    if extra.get("session_expired"):
                        await self._update_extra(agent_id, {"session_expired": False})
                    retry_delay = 2

                    new_cursor = str(data.get("get_updates_buf") or "")
                    if new_cursor and new_cursor != cursor:
                        await self._update_extra(agent_id, {"get_updates_buf": new_cursor})

                    for msg in data.get("msgs", []) or []:
                        try:
                            await process_wechat_message(agent_id, msg, config)
                        except Exception as exc:
                            logger.error(f"[WeChat] Failed to process message for {agent_id}: {exc}")
                except WeChatSessionExpiredError:
                    logger.warning(f"[WeChat] Session expired for agent {agent_id}")
                    await self._set_connected(agent_id, False)
                    await self._update_extra(agent_id, {"get_updates_buf": "", "session_expired": True})
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._connected[agent_id] = False
                    await self._set_connected(agent_id, False)
                    logger.error(f"[WeChat] Poll error for agent {agent_id}: {exc}")
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_retry_delay)
        except asyncio.CancelledError:
            await self._set_connected(agent_id, False)
            raise

    async def _fetch_updates(self, *, token: str, base_url: str, cursor: str, route_tag: str | None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=40) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/ilink/bot/getupdates",
                headers=build_wechat_headers(token, route_tag=route_tag),
                json={
                    "get_updates_buf": cursor,
                    "base_info": {
                        "channel_version": WECHAT_CHANNEL_VERSION,
                    },
                },
            )
            data = resp.json()
            if resp.status_code >= 400:
                raise RuntimeError(f"WeChat getupdates HTTP {resp.status_code}: {str(data)[:300]}")
            ret = data.get("ret", 0)
            errcode = data.get("errcode", 0)
            if ret == -14 or errcode == -14:
                raise WeChatSessionExpiredError(data.get("errmsg") or "session expired")
            if ret not in (0, None) or errcode not in (0, None):
                raise RuntimeError(data.get("errmsg") or f"WeChat getupdates failed: ret={ret}, errcode={errcode}")
            return data

    async def _load_config(self, agent_id: uuid.UUID):
        return await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="wechat")

    async def _update_extra(self, agent_id: uuid.UUID, updates: dict[str, Any]) -> None:
        config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="wechat")
        if not config:
            return
        extra = dict(config.extra_config or {})
        extra.update(updates)
        await channel_config_dao.update(db_obj=config, obj_in={"extra_config": extra})

    async def _set_connected(self, agent_id: uuid.UUID, connected: bool) -> None:
        config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="wechat")
        if not config:
            return
        await channel_config_dao.update(db_obj=config, obj_in={"is_connected": connected})


wechat_poll_manager = WeChatPollManager()
