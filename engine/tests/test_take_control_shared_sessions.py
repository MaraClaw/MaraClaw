import uuid
from datetime import UTC, datetime, timedelta

import anyio
import pytest

from app.api import agentbay_control as control
from app.services import agentbay_client, agentbay_shared_sessions as shared, take_control_state as state
from take_control_fakes import SharedRedis
from take_control_sdk_fakes import install


async def test_second_process_reattaches_same_page_and_initializes_local_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = SharedRedis()
    store = install(monkeypatch, redis)
    key = state.SessionKey(str(uuid.uuid4()), "chat", "browser")
    worker_cache: shared.ClientCache = {}
    api_cache: shared.ClientCache = {}
    worker = await shared.get_client(key, worker_cache, create=True)
    store.remotes[0].page = "https://portal.example/captcha"

    api = await shared.get_client(key, api_cache, create=False)

    assert api is not worker
    assert len(store.remotes) == 1
    assert store.attached == [store.remotes[0].session_id]
    assert worker._require_session().session_id == api._require_session().session_id
    assert worker._browser_initialized
    assert api._browser_initialized
    assert (await control._eval_cdp_script(api, "console.log('URL_OK:')"))["output"] == (
        "URL_OK:https://portal.example/captcha"
    )


@pytest.mark.parametrize("creator", [True, False])
async def test_local_expiry_detaches_without_deleting_leased_remote(
    monkeypatch: pytest.MonkeyPatch, creator: bool
) -> None:
    redis = SharedRedis()
    store = install(monkeypatch, redis)
    key = state.SessionKey(str(uuid.uuid4()), "chat", "browser")
    worker_cache: shared.ClientCache = {}
    worker = await shared.get_client(key, worker_cache, create=True)
    cache = worker_cache if creator else {}
    client = worker if creator else await shared.get_client(key, cache, create=False)
    await state.acquire(key, "owner", None)
    for cache_key in cache:
        cache[cache_key] = (client, datetime.now(UTC) - timedelta(minutes=6))

    await shared.cleanup(cache)

    assert cache == {}
    assert client._session is None
    assert not store.remotes[0].deleted
    assert await state.is_session_locked(key.agent_id, key.chat_id)


async def test_reattached_handle_never_deletes_idle_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = SharedRedis()
    store = install(monkeypatch, redis)
    key = state.SessionKey(str(uuid.uuid4()), "chat", "browser")
    await shared.get_client(key, {}, create=True)
    api = await shared.get_client(key, {}, create=False)
    redis.advance(301)

    await api.close_session()

    assert not store.remotes[0].deleted
    assert await state.get_remote(key) is not None


async def test_creator_retires_idle_remote_and_registry_together(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = SharedRedis()
    store = install(monkeypatch, redis)
    key = state.SessionKey(str(uuid.uuid4()), "chat", "browser")
    creator = await shared.get_client(key, {}, create=True)
    redis.advance(301)

    await creator.close_session()

    assert store.remotes[0].deleted
    assert await state.get_remote(key) is None
    with pytest.raises(state.SessionUnavailableError):
        await shared.get_client(key, {}, create=False)


async def test_released_provider_session_is_forgotten_without_blank_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = SharedRedis()
    store = install(monkeypatch, redis)
    key = state.SessionKey(str(uuid.uuid4()), "chat", "browser")
    await shared.get_client(key, {}, create=True)
    store.remotes[0].deleted = True

    with pytest.raises(state.SessionUnavailableError):
        await shared.get_client(key, {}, create=False)

    assert key.registry_key not in redis.values
    assert len(store.remotes) == 1


async def test_registry_outage_refuses_cached_session_without_creating_or_deleting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = SharedRedis()
    store = install(monkeypatch, redis)
    key = state.SessionKey(str(uuid.uuid4()), "chat", "browser")
    cache: shared.ClientCache = {}
    await shared.get_client(key, cache, create=True)
    redis.available = False

    with pytest.raises(state.TakeControlUnavailableError):
        await shared.get_client(key, cache, create=True)

    assert len(store.remotes) == 1
    assert not store.remotes[0].deleted


async def test_expired_owner_cannot_release_replacement_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = SharedRedis()
    install(monkeypatch, redis)
    key = state.SessionKey(str(uuid.uuid4()), "chat", "browser")
    await state.register_remote(key, state.RemoteSession(remote_id="remote", image="browser_latest"))
    old, _ = await state.acquire(key, "first", None)
    redis.advance(121)
    replacement, _ = await state.acquire(key, "second", None)

    with pytest.raises(state.TakeControlLostError):
        await state.release(key, "first", old.owner_token)

    assert (await state.renew(key, "second", replacement.owner_token)).owner_token == replacement.owner_token


async def test_concurrent_creators_publish_one_remote_and_delete_only_loser(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = SharedRedis()
    store = install(monkeypatch, redis)
    key = state.SessionKey(str(uuid.uuid4()), "chat", "browser")
    register = state.register_remote
    ready = anyio.Event()
    candidates: list[str] = []
    clients: list[agentbay_client.AgentBayClient] = []

    async def register_together(key: state.SessionKey, remote: state.RemoteSession) -> state.RemoteSession:
        candidates.append(remote.remote_id)
        if len(candidates) == 2:
            ready.set()
        await ready.wait()
        return await register(key, remote)

    async def create() -> None:
        clients.append(await shared.get_client(key, {}, create=True))

    monkeypatch.setattr(state, "register_remote", register_together)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(create)
        tasks.start_soon(create)

    assert len({client._require_session().session_id for client in clients}) == 1
    assert len(store.remotes) == 2
    assert sum(remote.deleted for remote in store.remotes) == 1


async def test_replicas_use_distinct_remote_action_script_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = SharedRedis()
    store = install(monkeypatch, redis)
    key = state.SessionKey(str(uuid.uuid4()), "chat", "browser")
    first = await shared.get_client(key, {}, create=True)
    second = await shared.get_client(key, {}, create=False)
    results: list[bool] = []

    async def action(client: agentbay_client.AgentBayClient) -> None:
        result = await control._eval_cdp_script(client, "console.log('TC_OK')")
        results.append(result["success"])

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(action, first)
        tasks.start_soon(action, second)

    assert results == [True, True]
    assert len(set(store.remotes[0].executed)) == 2
