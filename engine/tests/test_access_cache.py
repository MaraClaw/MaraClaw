"""Request memo + Redis decision cache for check_agent_access."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import access_cache, permissions
from app.core.access_cache import bump_agent_acl_version


class _FakePipeline:
    def __init__(self, store: dict[str, str]) -> None:
        self._store = store
        self._ops: list[tuple[str, str]] = []

    def get(self, key: str) -> None:
        self._ops.append(("get", key))

    async def execute(self) -> list[str | None]:
        return [self._store.get(key) for _op, key in self._ops]


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.fail = False

    def pipeline(self) -> _FakePipeline:
        if self.fail:
            raise RuntimeError("redis down")
        return _FakePipeline(self.store)

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        del ex
        self.store[key] = value

    async def incr(self, key: str) -> int:
        if self.fail:
            raise RuntimeError("redis down")
        nxt = int(self.store.get(key) or 0) + 1
        self.store[key] = str(nxt)
        return nxt

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)


@pytest.fixture(autouse=True)
def _reset_memo_and_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    access_cache.clear_request_memo()
    access_cache._deferred_acl.set(None)
    monkeypatch.setattr(access_cache, "_ttl_seconds", lambda: 45)
    yield
    access_cache.clear_request_memo()
    access_cache._deferred_acl.set(None)


def _user(**overrides):
    values = {
        "id": uuid.uuid4(),
        "role": "member",
        "tenant_id": uuid.uuid4(),
        "is_active": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _agent(**overrides):
    tenant = overrides.pop("tenant_id", uuid.uuid4())
    values = {
        "id": uuid.uuid4(),
        "creator_id": uuid.uuid4(),
        "tenant_id": tenant,
        "access_mode": "company",
        "company_access_level": "use",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_decide_creator_and_admin_and_custom() -> None:
    tenant = uuid.uuid4()
    creator = _user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant, creator_id=creator.id, access_mode="custom", company_access_level=None)
    assert permissions.decide_agent_access(creator, agent) == "manage"

    admin = _user(role="org_admin", tenant_id=tenant)
    public = _agent(tenant_id=tenant, access_mode="company", company_access_level="use")
    assert permissions.decide_agent_access(admin, public) == "manage"

    other = _user(tenant_id=tenant)
    perms = [SimpleNamespace(scope_type="user", scope_id=other.id, access_level="manage")]
    custom = _agent(tenant_id=tenant, access_mode="custom", company_access_level=None)
    assert permissions.decide_agent_access(other, custom, perms) == "manage"
    assert permissions.decide_agent_access(other, custom, []) is None


@pytest.mark.asyncio
async def test_request_memo_loads_agent_once(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = uuid.uuid4()
    user = _user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant, creator_id=user.id)
    gets = {"n": 0}

    async def fake_get(_id):
        gets["n"] += 1
        return agent

    monkeypatch.setattr(permissions.agent_dao, "get", fake_get)

    first = await permissions.check_agent_access(user, agent.id)
    second = await permissions.check_agent_access(user, agent.id)
    assert first == second
    assert first[1] == "manage"
    assert gets["n"] == 1


@pytest.mark.asyncio
async def test_redis_hit_skips_permission_list(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = uuid.uuid4()
    user = _user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant, access_mode="custom", company_access_level=None)
    fake = FakeRedis()
    listed = {"n": 0}

    async def fake_get(_id):
        return agent

    async def list_for_agent(_id):
        listed["n"] += 1
        return [SimpleNamespace(scope_type="user", scope_id=user.id, access_level="use")]

    async def get_redis():
        return fake

    monkeypatch.setattr(permissions.agent_dao, "get", fake_get)
    monkeypatch.setattr(permissions.agent_permission_dao, "list_for_agent", list_for_agent)
    monkeypatch.setattr(access_cache, "get_redis", get_redis)

    access_cache.clear_request_memo()
    first = await permissions.check_agent_access(user, agent.id)
    access_cache.clear_request_memo()
    second = await permissions.check_agent_access(user, agent.id)
    assert first[1] == "use"
    assert second[1] == "use"
    assert listed["n"] == 1


@pytest.mark.asyncio
async def test_role_change_misses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = uuid.uuid4()
    user = _user(role="member", tenant_id=tenant)
    agent = _agent(tenant_id=tenant, access_mode="company", company_access_level="use")
    fake = FakeRedis()

    async def fake_get(_id):
        return agent

    async def get_redis():
        return fake

    monkeypatch.setattr(permissions.agent_dao, "get", fake_get)
    monkeypatch.setattr(access_cache, "get_redis", get_redis)

    await permissions.check_agent_access(user, agent.id)
    access_cache.clear_request_memo()
    user.role = "org_admin"
    agent.access_mode = "company"
    level = await access_cache.get_cached_level(user, agent.id)
    assert level is None


@pytest.mark.asyncio
async def test_version_bump_invalidates_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = uuid.uuid4()
    user = _user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant, access_mode="custom", company_access_level=None)
    fake = FakeRedis()

    async def fake_get(_id):
        return agent

    async def list_for_agent(_id):
        return [SimpleNamespace(scope_type="user", scope_id=user.id, access_level="manage")]

    async def get_redis():
        return fake

    monkeypatch.setattr(permissions.agent_dao, "get", fake_get)
    monkeypatch.setattr(permissions.agent_permission_dao, "list_for_agent", list_for_agent)
    monkeypatch.setattr(access_cache, "get_redis", get_redis)

    await permissions.check_agent_access(user, agent.id)
    access_cache.clear_request_memo()
    await bump_agent_acl_version(agent.id)
    assert await access_cache.get_cached_level(user, agent.id) is None


@pytest.mark.asyncio
async def test_redis_failure_falls_open(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = uuid.uuid4()
    user = _user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant, creator_id=user.id)
    fake = FakeRedis()
    fake.fail = True

    async def fake_get(_id):
        return agent

    async def get_redis():
        return fake

    monkeypatch.setattr(permissions.agent_dao, "get", fake_get)
    monkeypatch.setattr(access_cache, "get_redis", get_redis)

    agent_row, level = await permissions.check_agent_access(user, agent.id)
    assert agent_row is agent
    assert level == "manage"


@pytest.mark.asyncio
async def test_ttl_zero_skips_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access_cache, "_ttl_seconds", lambda: 0)
    user = _user()
    assert await access_cache.get_cached_level(user, uuid.uuid4()) is None
    await access_cache.set_cached_level(user, uuid.uuid4(), "manage")
    await bump_agent_acl_version(uuid.uuid4())


@pytest.mark.asyncio
async def test_denied_access_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = uuid.uuid4()
    user = _user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant, access_mode="custom", company_access_level=None)
    fake = FakeRedis()

    async def fake_get(_id):
        return agent

    async def list_for_agent(_id):
        return []

    async def get_redis():
        return fake

    monkeypatch.setattr(permissions.agent_dao, "get", fake_get)
    monkeypatch.setattr(permissions.agent_permission_dao, "list_for_agent", list_for_agent)
    monkeypatch.setattr(access_cache, "get_redis", get_redis)

    with pytest.raises(HTTPException) as exc:
        await permissions.check_agent_access(user, agent.id)
    assert exc.value.status_code == 403
    assert fake.store == {}


@pytest.mark.asyncio
async def test_set_cached_level_skips_when_version_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = uuid.uuid4()
    user = _user(tenant_id=tenant)
    agent_id = uuid.uuid4()
    fake = FakeRedis()

    async def get_redis():
        return fake

    monkeypatch.setattr(access_cache, "get_redis", get_redis)
    await access_cache.set_cached_level(user, agent_id, "use", observed_ver="0")
    assert any(key.startswith("acl:v1:") for key in fake.store)
    fake.store.clear()
    await access_cache.set_cached_level(user, agent_id, "use", observed_ver="9")
    assert fake.store == {}


@pytest.mark.asyncio
async def test_permission_delete_bumps_after_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.dao.agent_dao import agent_permission_dao
    from app.db import session as session_module
    from app.db.session import connection_ctx

    agent_id = uuid.uuid4()
    fake = FakeRedis()
    raw = _SessionRaw()

    async def get_redis():
        return fake

    monkeypatch.setattr(access_cache, "get_redis", get_redis)
    monkeypatch.setattr(session_module, "get_pool", lambda: _SessionPool(raw))
    token = session_module._conn_ctx.set(None)
    try:
        async with connection_ctx():
            await agent_permission_dao.delete_for_agent(agent_id)
            assert all(not key.startswith("aclver:") for key in fake.store)
        ver_key = f"aclver:{agent_id}"
        assert fake.store.get(ver_key) == "1"
    finally:
        session_module._conn_ctx.reset(token)


class _SessionCursor:
    def __init__(self, parent: _SessionRaw) -> None:
        self._parent = parent

    async def __aenter__(self) -> _SessionCursor:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, query: str, params: object = None) -> None:
        self._parent.executed.append(query)
        del params

    async def fetchone(self) -> None:
        return None

    async def fetchall(self) -> list[object]:
        return []


class _SessionRaw:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.commits = 0

    def cursor(self) -> _SessionCursor:
        return _SessionCursor(self)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


class _SessionPoolCM:
    def __init__(self, raw: _SessionRaw) -> None:
        self._raw = raw

    async def __aenter__(self) -> _SessionRaw:
        return self._raw

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _SessionPool:
    def __init__(self, raw: _SessionRaw) -> None:
        self._raw = raw

    def connection(self) -> _SessionPoolCM:
        return _SessionPoolCM(self._raw)
