"""SQL-shape and unit tests for remaining hot-path work (no live Postgres)."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_message_dao
from app.db import pool as pool_module, session as session_module
from app.services import agent_context, agent_context_cache, chat_persist
from app.services.llm import caller
from app.services.llm.turn import TurnContext


class _FakeCursor:
    def __init__(self, parent: _FakeRawConnection) -> None:
        self._parent = parent

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, query: str, params: Any = None) -> None:
        self._parent.executed.append((query, params))
        self._parent.last_params = params or {}

    async def fetchone(self) -> dict[str, Any]:
        params = dict(self._parent.last_params)
        row_id = params.get("id") or uuid4()
        user_id = params.get("user_id") or uuid4()
        agent_id = params.get("agent_id") or uuid4()
        return {
            "id": row_id,
            "agent_id": agent_id,
            "user_id": user_id,
            "role": params.get("role") or "assistant",
            "content": params.get("content") or "",
            "conversation_id": params.get("conversation_id") or str(row_id),
            "participant_id": params.get("participant_id"),
            "thinking": params.get("thinking"),
            "created_at": params.get("created_at"),
            "title": params.get("title") or "Session test",
            "source_channel": params.get("source_channel") or "web",
            "external_conv_id": params.get("external_conv_id"),
            "is_group": params.get("is_group", False),
            "group_name": params.get("group_name"),
            "peer_agent_id": params.get("peer_agent_id"),
            "is_primary": params.get("is_primary", False),
            "last_read_at_by_user": params.get("last_read_at_by_user"),
            "last_message_at": params.get("last_message_at"),
            "name": params.get("name") or "Agent",
            "creator_id": params.get("creator_id") or user_id,
            "last_active_at": params.get("last_active_at"),
        }

    async def fetchall(self) -> list[Any]:
        return []


class _FakeRawConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.commits = 0
        self.last_params: dict[str, Any] = {}

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


class _PoolConnectionCM:
    def __init__(self, raw: _FakeRawConnection) -> None:
        self._raw = raw

    async def __aenter__(self) -> _FakeRawConnection:
        return self._raw

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _FakePool:
    def __init__(self, raw: _FakeRawConnection) -> None:
        self._raw = raw

    def connection(self) -> _PoolConnectionCM:
        return _PoolConnectionCM(self._raw)


@pytest.fixture
def raw(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeRawConnection]:
    conn = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(conn))
    token = session_module._conn_ctx.set(None)
    yield conn
    session_module._conn_ctx.reset(token)


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.fail = False

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        del ex
        self.store[key] = value

    async def delete(self, *keys: str) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        for key in keys:
            self.store.pop(key, None)

    async def incr(self, key: str) -> int:
        if self.fail:
            raise RuntimeError("redis down")
        nxt = int(self.store.get(key) or 0) + 1
        self.store[key] = str(nxt)
        return nxt


@pytest.mark.asyncio
async def test_call_agent_llm_skips_dao_when_turn_preloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    gets: list[str] = []

    async def boom(*_args: object, **_kwargs: object) -> None:
        gets.append("get")
        raise AssertionError("DAO get should not run when TurnContext is preloaded")

    async def boom_many(*_args: object, **_kwargs: object) -> list[object]:
        gets.append("get_many")
        raise AssertionError("llm_model_dao.get_many should not run when models are preloaded")

    async def fake_failover(**_kwargs: object) -> str:
        return "ok"

    from app.dao.llm_dao import llm_model_dao

    monkeypatch.setattr(agent_dao, "get", boom)
    monkeypatch.setattr(llm_model_dao, "get", boom)
    monkeypatch.setattr(llm_model_dao, "get_many", boom_many)
    monkeypatch.setattr(caller, "call_llm_with_failover", fake_failover)

    agent = SimpleNamespace(
        id=uuid4(),
        name="Agent",
        role_description="role",
        primary_model_id=uuid4(),
        fallback_model_id=uuid4(),
        is_expired=False,
        expires_at=None,
    )
    primary = SimpleNamespace(model="p", supports_vision=False)
    fallback = SimpleNamespace(model="f", supports_vision=False)
    result = await caller.call_agent_llm(
        None,
        agent.id,
        "hello",
        turn=TurnContext(agent=agent, primary_model=primary, fallback_model=fallback),
    )
    assert result == "ok"
    assert gets == []


@pytest.mark.asyncio
async def test_persist_chat_message_one_commit(raw: _FakeRawConnection) -> None:
    await chat_persist.persist_chat_message(
        agent_id=uuid4(),
        user_id=uuid4(),
        conversation_id=str(uuid4()),
        role="assistant",
        content="hi",
        touch_last_active=True,
    )
    assert raw.commits == 1
    statements = [q for q, _p in raw.executed]
    assert any("INSERT INTO chat_messages" in q for q in statements)
    assert any("last_active_at" in q for q in statements)


@pytest.mark.asyncio
async def test_persist_skip_insert_does_not_write_message(raw: _FakeRawConnection) -> None:
    await chat_persist.persist_chat_message(
        agent_id=uuid4(),
        user_id=uuid4(),
        conversation_id=str(uuid4()),
        role="user",
        content="",
        skip_insert=True,
        title_if_default="Onboarding",
    )
    assert raw.commits == 1
    statements = [q for q, _p in raw.executed]
    update_params = [p or {} for q, p in raw.executed if q.lstrip().startswith("UPDATE")]
    assert not any("INSERT INTO chat_messages" in q for q in statements)
    assert all("last_message_at" not in p for p in update_params)
    assert any("title" in p for p in update_params)


@pytest.mark.asyncio
async def test_call_agent_llm_loads_missing_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    fallback_id = uuid4()
    loaded_ids: list[list[object]] = []

    async def fake_get_many(ids):
        loaded_ids.append(list(ids))
        return [SimpleNamespace(id=fallback_id, model="fb", supports_vision=False)]

    async def fake_failover(**kwargs: object) -> str:
        assert kwargs.get("fallback_model") is not None
        return "ok"

    from app.dao.llm_dao import llm_model_dao

    monkeypatch.setattr(llm_model_dao, "get_many", fake_get_many)
    monkeypatch.setattr(caller, "call_llm_with_failover", fake_failover)

    agent = SimpleNamespace(
        id=uuid4(),
        name="Agent",
        role_description="role",
        primary_model_id=uuid4(),
        fallback_model_id=fallback_id,
        is_expired=False,
        expires_at=None,
    )
    primary = SimpleNamespace(model="p", supports_vision=False)
    result = await caller.call_agent_llm(
        None,
        agent.id,
        "hello",
        turn=TurnContext(agent=agent, primary_model=primary, fallback_model=None),
    )
    assert result == "ok"
    assert loaded_ids == [[fallback_id]]


@pytest.mark.asyncio
async def test_get_agent_config_reloads_token_counters(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid4()
    handshake = SimpleNamespace(
        max_tool_rounds=7,
        max_tokens_per_day=10,
        tokens_used_today=1,
        max_tokens_per_month=None,
        tokens_used_month=0,
    )
    fresh = SimpleNamespace(
        max_tool_rounds=99,
        max_tokens_per_day=10,
        tokens_used_today=10,
        max_tokens_per_month=None,
        tokens_used_month=0,
    )

    async def fake_get(_id: object) -> object:
        return fresh

    monkeypatch.setattr(agent_dao, "get", fake_get)
    rounds, msg = await caller._get_agent_config(agent_id, agent=handshake)
    assert rounds == 7
    assert msg is not None
    assert "Daily token usage" in msg


@pytest.mark.asyncio
async def test_build_agent_context_second_call_skips_storage_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _FakeRedis()
    reads = {"n": 0}

    async def fake_redis() -> _FakeRedis:
        return redis

    async def fake_read(key: str, max_chars: int = 3000) -> str:
        del max_chars
        reads["n"] += 1
        if key.endswith("soul.md"):
            return "soul body"
        if "memory" in key:
            return "memory body"
        return ""

    async def fake_skills(_agent_id: object) -> str:
        reads["n"] += 1
        return "skills index"

    async def fake_rels(_agent_id: object) -> str:
        return ""

    async def fake_tz(_agent_id: object) -> str:
        return "UTC"

    monkeypatch.setattr(agent_context_cache, "get_redis", fake_redis)
    monkeypatch.setattr(agent_context_cache, "_ttl", lambda: 60)
    monkeypatch.setattr(agent_context, "_read_file_safe", fake_read)
    monkeypatch.setattr(agent_context, "_load_skills_index", fake_skills)
    monkeypatch.setattr(agent_context, "_load_relationships_from_db", fake_rels)
    monkeypatch.setattr("app.services.timezone_utils.get_agent_timezone", fake_tz)

    agent_id = uuid4()
    first = await agent_context.build_agent_context(agent_id, "Name", "role")
    after_first = reads["n"]
    assert after_first >= 1
    second = await agent_context.build_agent_context(agent_id, "Name", "role")
    assert reads["n"] == after_first
    assert first[0] == second[0]


@pytest.mark.asyncio
async def test_build_agent_context_redis_failure_falls_open(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _FakeRedis()
    redis.fail = True

    async def fake_redis() -> _FakeRedis:
        return redis

    async def fake_read(key: str, max_chars: int = 3000) -> str:
        del max_chars
        return "soul body" if key.endswith("soul.md") else ""

    async def fake_skills(_agent_id: object) -> str:
        return ""

    async def fake_rels(_agent_id: object) -> str:
        return ""

    async def fake_tz(_agent_id: object) -> str:
        return "UTC"

    monkeypatch.setattr(agent_context_cache, "get_redis", fake_redis)
    monkeypatch.setattr(agent_context_cache, "_ttl", lambda: 60)
    monkeypatch.setattr(agent_context, "_read_file_safe", fake_read)
    monkeypatch.setattr(agent_context, "_load_skills_index", fake_skills)
    monkeypatch.setattr(agent_context, "_load_relationships_from_db", fake_rels)
    monkeypatch.setattr("app.services.timezone_utils.get_agent_timezone", fake_tz)

    static, _dynamic = await agent_context.build_agent_context(uuid4(), "Name", "role")
    assert "You are Name" in static


@pytest.mark.asyncio
async def test_context_cache_invalidates_on_soul_path(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _FakeRedis()
    reads = {"n": 0}

    async def fake_redis() -> _FakeRedis:
        return redis

    async def fake_read(key: str, max_chars: int = 3000) -> str:
        del max_chars
        reads["n"] += 1
        return "soul body" if key.endswith("soul.md") else ""

    async def fake_skills(_agent_id: object) -> str:
        return ""

    async def fake_rels(_agent_id: object) -> str:
        return ""

    async def fake_tz(_agent_id: object) -> str:
        return "UTC"

    monkeypatch.setattr(agent_context_cache, "get_redis", fake_redis)
    monkeypatch.setattr(agent_context_cache, "_ttl", lambda: 60)
    monkeypatch.setattr(agent_context, "_read_file_safe", fake_read)
    monkeypatch.setattr(agent_context, "_load_skills_index", fake_skills)
    monkeypatch.setattr(agent_context, "_load_relationships_from_db", fake_rels)
    monkeypatch.setattr("app.services.timezone_utils.get_agent_timezone", fake_tz)

    agent_id = uuid4()
    await agent_context.build_agent_context(agent_id, "Name", "role")
    after_first = reads["n"]
    await agent_context_cache.invalidate_for_workspace_paths(agent_id, "soul.md")
    await agent_context.build_agent_context(agent_id, "Name", "role")
    assert reads["n"] > after_first


@pytest.mark.asyncio
async def test_context_cache_rejects_stale_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _FakeRedis()

    async def fake_redis() -> _FakeRedis:
        return redis

    monkeypatch.setattr(agent_context_cache, "get_redis", fake_redis)
    monkeypatch.setattr(agent_context_cache, "_ttl", lambda: 60)

    agent_id = uuid4()
    _text, observed = await agent_context_cache.read_cached_text(agent_id, "soul")
    assert _text is None
    await agent_context_cache.invalidate_agent_context(agent_id, "soul")
    await agent_context_cache.set_cached_text(agent_id, "soul", "stale", observed_ver=observed)
    assert await agent_context_cache.get_cached_text(agent_id, "soul") is None


@pytest.mark.asyncio
async def test_latest_contents_uses_window(raw: _FakeRawConnection) -> None:
    result = await chat_message_dao.latest_contents(
        agent_id=uuid4(),
        conversation_ids=["a", "b"],
    )
    assert result == {}
    query, params = raw.executed[0]
    assert "ROW_NUMBER()" in query
    assert "ANY(%(ids)s)" in query
    assert params["ids"] == ["a", "b"]


@pytest.mark.asyncio
async def test_apply_token_counter_resets_many_batches(raw: _FakeRawConnection) -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    agents = [
        SimpleNamespace(
            id=uuid4(),
            last_daily_reset=now - timedelta(days=2),
            last_monthly_reset=now.replace(year=now.year - 1) if now.month == 1 else now.replace(month=1),
            tokens_used_today=9,
            cache_read_tokens_today=1,
            cache_creation_tokens_today=1,
            tokens_used_month=9,
            cache_read_tokens_month=1,
            cache_creation_tokens_month=1,
        )
    ]
    await agent_dao.apply_token_counter_resets_many(agents)
    updates = [q for q, _p in raw.executed if q.lstrip().startswith("UPDATE agents")]
    assert len(updates) == 2
    assert all("ANY(%(ids)s)" in q for q in updates)
    assert agents[0].tokens_used_today == 0
    assert agents[0].tokens_used_month == 0


@pytest.mark.asyncio
async def test_list_heartbeat_candidates_selects_claim_columns(raw: _FakeRawConnection) -> None:
    await agent_dao.list_heartbeat_candidates()
    query, _params = raw.executed[0]
    assert "FROM agents" in query
    assert "heartbeat_enabled" in query
    assert "tokens_used_today" not in query
    assert "autonomy_policy" not in query
    assert "api_key_hash" not in query


@pytest.mark.asyncio
async def test_catalog_defaults_use_one_channel_query(raw: _FakeRawConnection, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.tool_runtime import catalog

    calls = {"n": 0}
    real = catalog._channel_presence

    async def wrapped(agent_id):
        calls["n"] += 1
        return await real(agent_id)

    monkeypatch.setattr(catalog, "_channel_presence", wrapped)
    deps = SimpleNamespace(
        agent_has_feishu=catalog.agent_has_feishu,
        agent_has_any_channel=catalog.agent_has_any_channel,
    )
    await catalog._resolve_channel_presence(uuid4(), deps)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_init_pool_passes_hygiene_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class FakePool:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args
            captured.update(kwargs)

        async def open(self) -> None:
            return None

    pool_module.reset_pool_for_tests()
    monkeypatch.setattr(pool_module, "AsyncConnectionPool", FakePool)
    monkeypatch.setattr(
        pool_module,
        "get_settings",
        lambda: SimpleNamespace(
            DATABASE_URL="postgresql://u:p@localhost/db",
            DATABASE_POOL_MIN_SIZE=1,
            DATABASE_POOL_MAX_SIZE=2,
            DATABASE_POOL_TIMEOUT=5.0,
            DATABASE_POOL_MAX_IDLE=600.0,
            DATABASE_POOL_MAX_LIFETIME=1800.0,
        ),
    )
    try:
        await pool_module.init_pool()
        assert captured["max_idle"] == 600.0
        assert captured["max_lifetime"] == 1800.0
        assert captured["check"] is pool_module._check_pooled_connection
        closed = SimpleNamespace(closed=True)
        with pytest.raises(OSError, match="closed"):
            await pool_module._check_pooled_connection(closed)
        await pool_module._check_pooled_connection(SimpleNamespace(closed=False))
    finally:
        pool_module.reset_pool_for_tests()
