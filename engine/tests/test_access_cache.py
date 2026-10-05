"""Request memo + Redis decision cache for check_agent_access."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.core import access_cache, permissions
from app.core.access_cache import bump_agent_acl_version
from app.records.agent import AgentPermissionRecord, AgentRecord
from app.records.user import UserRecord


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

    async def eval(self, script, key_count, version_key, decision_key, expected, payload, ttl):
        del script, key_count, ttl
        if self.fail:
            raise RuntimeError("redis down")
        if self.store.get(version_key, "0") != expected:
            return 0
        self.store[decision_key] = payload
        return 1


@pytest.fixture(autouse=True)
def _reset_memo_and_ttl(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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
    return UserRecord(**values)


def _agent(**overrides):
    tenant = overrides.pop("tenant_id", uuid.uuid4())
    values = {
        "id": uuid.uuid4(),
        "name": "Test agent",
        "creator_id": uuid.uuid4(),
        "tenant_id": tenant,
        "access_mode": "company",
        "company_access_level": "use",
    }
    values.update(overrides)
    return AgentRecord(**values)


def test_decide_creator_and_admin_and_custom() -> None:
    tenant = uuid.uuid4()
    creator = _user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant, creator_id=creator.id, access_mode="custom", company_access_level=None)
    assert permissions.decide_agent_access(creator, agent) == "manage"

    admin = _user(role="org_admin", tenant_id=tenant)
    public = _agent(tenant_id=tenant, access_mode="company", company_access_level="use")
    assert permissions.decide_agent_access(admin, public) == "manage"

    other = _user(tenant_id=tenant)
    perms = [
        AgentPermissionRecord(
            id=uuid.uuid4(), agent_id=agent.id, scope_type="user", scope_id=other.id, access_level="manage"
        )
    ]
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

    monkeypatch.setattr(permissions.user_dao, "get", AsyncMock(return_value=user))
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
    monkeypatch.setattr(permissions.user_dao, "get", AsyncMock(return_value=user))
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
    monkeypatch.setattr(permissions.user_dao, "get", AsyncMock(return_value=user))
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
    monkeypatch.setattr(permissions.user_dao, "get", AsyncMock(return_value=user))
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
    monkeypatch.setattr(permissions.user_dao, "get", AsyncMock(return_value=user))
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

    async def has_department_access(_user_id, _agent_id):
        return False

    monkeypatch.setattr(permissions.local_department_dao, "has_agent_access", has_department_access)
    monkeypatch.setattr(permissions.agent_dao, "get", fake_get)
    monkeypatch.setattr(permissions.user_dao, "get", AsyncMock(return_value=user))
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


@pytest.mark.asyncio
async def test_permission_rollback_never_publishes_acl_version(monkeypatch):
    from app.dao.agent_dao import agent_permission_dao
    from app.db import session as session_module

    agent_id = uuid.uuid4()
    fake = FakeRedis()
    raw = _SessionRaw()

    async def redis():
        return fake

    monkeypatch.setattr(access_cache, "get_redis", redis)
    monkeypatch.setattr(session_module, "get_pool", lambda: _SessionPool(raw))
    token = session_module._conn_ctx.set(None)

    async def abort_mutation():
        async with session_module.connection_ctx():
            await agent_permission_dao.delete_for_agent(agent_id)
            raise ValueError("abort mutation")

    try:
        with pytest.raises(ValueError):
            await abort_mutation()
        assert fake.store == {}
        assert raw.commits == 0
    finally:
        session_module._conn_ctx.reset(token)


@pytest.mark.asyncio
async def test_dirty_acl_inputs_are_never_cached(monkeypatch):
    fake = FakeRedis()
    agent_id = uuid.uuid4()
    user = _user()

    async def redis():
        return fake

    monkeypatch.setattr(access_cache, "get_redis", redis)
    await access_cache.set_cached_level(user, agent_id, "use", observed_ver="0")
    assert await access_cache.get_cached_level(user, agent_id) == "use"
    token = access_cache.begin_deferred_acl()
    try:
        await access_cache.bump_agent_acl_version(agent_id)
        assert await access_cache.get_cached_level(user, agent_id) is None
        await access_cache.set_cached_level(user, agent_id, "manage", observed_ver="0")
    finally:
        access_cache.end_deferred_acl(token)
    assert await access_cache.get_cached_level(user, agent_id) == "use"


@pytest.mark.asyncio
async def test_version_change_between_validation_and_write_rejects_stale_fill(monkeypatch):
    agent_id = uuid.uuid4()

    class ConcurrentRedis(FakeRedis):
        async def eval(self, script, key_count, version_key, decision_key, expected, payload, ttl):
            self.store[version_key] = "1"
            return await super().eval(script, key_count, version_key, decision_key, expected, payload, ttl)

    fake = ConcurrentRedis()

    async def redis():
        return fake

    monkeypatch.setattr(access_cache, "get_redis", redis)
    await access_cache.set_cached_level(_user(), agent_id, "use", observed_ver="0")
    assert not any(key.startswith("acl:v1:") for key in fake.store)


@pytest.mark.asyncio
@pytest.mark.parametrize("helper", ["request", "user_id"])
async def test_access_helpers_observe_version_before_decision_inputs(monkeypatch, helper):
    user = _user()
    agent = _agent(tenant_id=user.tenant_id)
    fake = FakeRedis()

    async def redis():
        return fake

    async def agent_input(_id):
        fake.store[f"aclver:{agent.id}"] = "1"
        return agent

    async def user_input(_id):
        if helper == "user_id":
            fake.store[f"aclver:{agent.id}"] = "1"
        return user

    monkeypatch.setattr(access_cache, "get_redis", redis)
    monkeypatch.setattr(permissions.agent_dao, "get", agent_input)
    monkeypatch.setattr(permissions.user_dao, "get", user_input)
    if helper == "request":
        result = (await permissions.check_agent_access(user, agent.id))[1]
    else:
        result = await permissions.get_agent_access_level_for_user_id(None, user.id, agent)
    assert result == "use"
    assert not any(key.startswith("acl:v1:") for key in fake.store)


@pytest.mark.asyncio
async def test_inactive_user_id_helper_rejects_even_a_request_memo(monkeypatch):
    user = _user(is_active=False)
    agent = _agent(tenant_id=user.tenant_id)

    async def user_input(_id):
        return user

    monkeypatch.setattr(permissions.user_dao, "get", user_input)
    access_cache.memo_set(user.id, agent.id, agent, "manage")
    assert await permissions.get_agent_access_level_for_user_id(None, user.id, agent) is None


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


@pytest.mark.asyncio
async def test_promotion_rollback_does_not_publish_session_or_acl_versions(monkeypatch):
    from dataclasses import replace

    from app.core import acl_locks
    from app.dao.base import BaseDAO
    from app.db import session

    user = _user()
    agent_id = uuid.uuid4()
    raw = _SessionRaw()
    fake = FakeRedis()
    monkeypatch.setattr(session, "get_pool", lambda: _SessionPool(raw))
    monkeypatch.setattr(access_cache, "get_redis", AsyncMock(return_value=fake))
    monkeypatch.setattr(acl_locks, "lock_tenants", AsyncMock())
    monkeypatch.setattr(acl_locks, "lock_user", AsyncMock())
    monkeypatch.setattr(BaseDAO, "update", AsyncMock(return_value=replace(user, role="org_admin")))

    async def invalidate(_db, _tenant):
        await bump_agent_acl_version(agent_id)

    monkeypatch.setattr(acl_locks, "invalidate_tenant_agents", invalidate)

    async def abort():
        async with session.connection_ctx():
            await permissions.user_dao.update(db_obj=user, obj_in={"role": "org_admin"})
            raise ValueError("rollback promotion")

    token = session._conn_ctx.set(None)
    try:
        with pytest.raises(ValueError):
            await abort()
        assert any("DELETE FROM local_department_memberships" in sql for sql in raw.executed)
        assert raw.commits == 0
        assert fake.store == {}
    finally:
        session._conn_ctx.reset(token)
