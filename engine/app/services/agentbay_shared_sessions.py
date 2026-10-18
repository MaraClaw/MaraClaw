"""Per-process handles for shared canonical AgentBay remote sessions."""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

import anyio

from app.core.json_types import json_as_str_or
from app.services import take_control_state as state
from app.services.agentbay_session_config import configured_client

if TYPE_CHECKING:
    from app.services.agentbay_client import AgentBayClient

type ClientCache = dict[tuple[uuid.UUID, str, str], tuple[AgentBayClient, datetime]]
_shared_clients: WeakKeyDictionary[AgentBayClient, tuple[state.SessionKey, bool]] = WeakKeyDictionary()


async def get_client(key: state.SessionKey, cache: ClientCache, *, create: bool) -> AgentBayClient:
    from app.services.agentbay_client import _AGENTBAY_SESSION_TIMEOUT, _inject_credentials

    cache_key = (uuid.UUID(key.agent_id), key.chat_id, key.image_type)
    remote = await state.get_remote(key)
    cached = cache.get(cache_key)
    now = datetime.now(UTC)
    if cached:
        client, last_used = cached
        session = client._session
        if (
            remote
            and session
            and session.session_id == remote.remote_id
            and now - last_used < _AGENTBAY_SESSION_TIMEOUT
        ):
            cache[cache_key] = (client, now)
            return client
        await close_client(client)
        del cache[cache_key]
    if remote is None and not create:
        raise state.SessionUnavailableError("No active session for this agent, chat and environment")
    client, config = await configured_client(cache_key[0])
    if remote is None:
        match key.image_type:
            case "browser":
                image = "browser_latest"
            case "computer":
                image = (
                    "windows_latest"
                    if json_as_str_or(config.get("os_type"), "windows") == "windows"
                    else "linux_latest"
                )
            case _:
                image = "code_latest"
        created = await client.create_session(image)
        candidate = state.RemoteSession(remote_id=created.session_id, image=image)
        if key.image_type == "browser":
            await _inject_credentials(client, cache_key[0])
        _shared_clients[client] = (key, True)
        try:
            remote = await state.register_remote(key, candidate)
        except state.TakeControlUnavailableError:
            client._session = None
            raise
        if remote.remote_id != candidate.remote_id:
            del _shared_clients[client]
            await close_client(client)
    if client._session is None:
        result = await anyio.to_thread.run_sync(client._sdk.get, remote.remote_id)
        if not result.success or result.session is None:
            if result.code == "InvalidMcpSession.NotFound":
                await state.forget_released_remote(key, remote.remote_id)
            raise state.SessionUnavailableError(f"Registered AgentBay session unavailable: {result.error_message}")
        client._session = result.session
        client._image_type = remote.image
        client._browser_initialized = False
        _shared_clients[client] = (key, False)
    if key.image_type == "browser":
        await client._ensure_browser_initialized()
    cache[cache_key] = (client, now)
    return client


async def close_client(client: AgentBayClient) -> None:
    session = client._session
    if session is None:
        return
    if client not in _shared_clients:
        await anyio.to_thread.run_sync(session.delete)
    else:
        key, created_here = _shared_clients[client]
        if created_here and await state.drop_remote(key, session.session_id):
            await anyio.to_thread.run_sync(session.delete)
    client._session = None
    client._browser_initialized = False


async def cleanup(cache: ClientCache) -> None:
    from app.services.agentbay_client import _AGENTBAY_SESSION_TIMEOUT

    now = datetime.now(UTC)
    for cache_key, (client, last_used) in list(cache.items()):
        if now - last_used > _AGENTBAY_SESSION_TIMEOUT:
            await close_client(client)
            del cache[cache_key]
