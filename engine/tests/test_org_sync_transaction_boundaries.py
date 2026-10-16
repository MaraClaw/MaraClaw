import asyncio
from copy import deepcopy
from dataclasses import replace
from typing import final, override
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.transaction_pool_fakes import CountingPool

from app.api import enterprise
from app.dao.user_dao import user_dao
from app.db import session
from app.db.connection import DbConnection
from app.records.identity import IdentityProviderRecord, IdentityRecord
from app.records.user import UserRecord
from app.services import org_sync
from app.services.org_sync.base import BaseOrgSyncAdapter
from app.services.org_sync.types import ExternalDepartment, ExternalUser


@final
class BoundaryAdapter(BaseOrgSyncAdapter):
    provider_type = "feishu"

    def __init__(self, provider):
        super().__init__(provider=provider, tenant_id=provider.tenant_id)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.writes = []
        self.effects = []
        self.fail_fetch = False
        self.fail_write = False

    @property
    @override
    def api_base_url(self):
        return "https://sync.invalid"

    @override
    async def get_access_token(self):
        return "token"

    @override
    async def fetch_departments(self):
        assert session.get_connection() is None
        return [ExternalDepartment(external_id="dept", name="Department")]

    @override
    async def fetch_users(self, department_external_id):
        assert session.get_connection() is None
        self.entered.set()
        await self.release.wait()
        if self.fail_fetch:
            raise RuntimeError("remote failed")
        return [ExternalUser(external_id="user", name="User", unionid="union")]

    async def _write(self, name):
        async with session.connection_ctx():
            self.writes.append((name, session.get_connection()))
            session.run_after_commit(lambda: self.effects.append(name))

    @override
    async def _upsert_department(self, provider, dept):
        await self._write("department")

    @override
    async def _rebuild_department_paths(self, provider_id):
        await self._write("paths")
        return {}

    @override
    async def _upsert_member(self, provider, user, department_external_id):
        await self._write("member")
        if self.fail_write:
            raise RuntimeError("write failed")
        return {"user_created": False, "profile_synced": True}

    @override
    async def _refresh_member_department_paths(self, provider_id):
        await self._write("member_paths")

    @override
    async def _reconcile(self, provider_id, sync_start):
        await self._write("reconcile")

    @override
    async def _update_member_counts(self, provider_id):
        await self._write("counts")


@pytest.fixture
def sync_case(monkeypatch):
    pool = CountingPool()
    provider = IdentityProviderRecord(
        id=uuid4(), tenant_id=uuid4(), provider_type="feishu", name="Provider", config={"credentials": {"key": "old"}}
    )
    identity = IdentityRecord(id=uuid4())
    actor = UserRecord(
        id=uuid4(), tenant_id=provider.tenant_id, identity_id=identity.id, identity=identity, role="org_admin"
    )
    adapter = BoundaryAdapter(deepcopy(provider))
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    monkeypatch.setattr(DbConnection, "execute", AsyncMock())
    monkeypatch.setattr(DbConnection, "fetchone", AsyncMock(return_value={"is_active": True}))
    monkeypatch.setattr(enterprise.identity_provider_dao, "get", AsyncMock(return_value=provider))
    monkeypatch.setattr(user_dao, "get_with_identity", AsyncMock(return_value=actor))
    monkeypatch.setitem(org_sync.SYNC_ADAPTER_CLASSES, "feishu", lambda **kwargs: adapter)

    async def update(**kwargs):
        await adapter._write("config")
        return kwargs["db_obj"]

    monkeypatch.setattr(enterprise.identity_provider_dao, "update", update)
    app = FastAPI()
    app.include_router(enterprise.router, prefix="/api")
    app.dependency_overrides[enterprise.get_current_admin] = lambda: actor
    return pool, provider, actor, adapter, app


async def post_sync(app, provider):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/api/enterprise/org/sync", params={"provider_id": str(provider.id)})


async def test_route_collects_without_checkout_then_commits_once(sync_case):
    pool, provider, _, adapter, app = sync_case
    task = asyncio.create_task(post_sync(app, provider))
    try:
        await asyncio.wait_for(adapter.entered.wait(), 2)
        assert pool.active == 0
        assert adapter.writes == []
        async with asyncio.timeout(2), session.connection_ctx():
            pass
        before = pool.raw.commits
        adapter.release.set()
        response = await task
    finally:
        adapter.release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert response.status_code == 200
    assert response.json()["members"] == 1
    assert pool.raw.commits - before == 1
    assert len({id(connection) for _, connection in adapter.writes}) == 1
    assert adapter.effects == [name for name, _ in adapter.writes]


async def test_route_cancel_during_users_has_no_writes_or_effects(sync_case):
    pool, provider, _, adapter, app = sync_case
    task = asyncio.create_task(post_sync(app, provider))
    await asyncio.wait_for(adapter.entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.writes == []
    assert adapter.effects == []
    assert pool.raw.commits == 0
    assert pool.active == 0


@pytest.mark.parametrize(
    "change",
    [
        "actor",
        "identity",
        "identity_id",
        "membership",
        "role",
        "password",
        "tenant",
        "missing",
        "inactive",
        "provider_tenant",
        "type",
        "config",
    ],
)
async def test_route_rejects_authority_drift_before_writing(monkeypatch, sync_case, change):
    pool, provider, actor, adapter, app = sync_case
    task = asyncio.create_task(post_sync(app, provider))
    await asyncio.wait_for(adapter.entered.wait(), 2)
    fresh = actor
    current = provider
    if change == "actor":
        fresh = replace(actor, is_active=False)
    elif change == "identity":
        fresh = replace(actor, identity=replace(actor.identity, is_active=False))
    elif change == "identity_id":
        fresh = replace(actor, identity_id=uuid4())
    elif change == "membership":
        fresh = replace(actor, tenant_id=uuid4())
    elif change == "role":
        fresh = replace(actor, role="member")
    elif change == "password":
        fresh = replace(actor, identity=replace(actor.identity, must_change_password=True))
    elif change == "tenant":
        monkeypatch.setattr(DbConnection, "fetchone", AsyncMock(return_value={"is_active": False}))
    elif change == "missing":
        current = None
    elif change == "inactive":
        current = replace(provider, is_active=False)
    elif change == "provider_tenant":
        current = replace(provider, tenant_id=uuid4())
    elif change == "type":
        current = replace(provider, provider_type="wecom")
    elif change == "config":
        provider.config["credentials"]["key"] = "rotated"
        assert adapter.config["credentials"]["key"] == "old"
    monkeypatch.setattr(user_dao, "get_with_identity", AsyncMock(return_value=fresh))
    monkeypatch.setattr(enterprise.identity_provider_dao, "get", AsyncMock(return_value=current))
    adapter.release.set()
    response = await task
    assert response.status_code in {403, 404, 409}
    assert adapter.writes == []
    assert adapter.effects == []
    assert pool.raw.commits == 0
    assert pool.active == 0


async def test_adapter_does_not_ensure_provider_before_remote_collection(monkeypatch, sync_case):
    pool, provider, _, adapter, _ = sync_case
    adapter.provider = None
    ensure = AsyncMock(return_value=provider)
    monkeypatch.setattr(adapter, "_ensure_provider", ensure)
    task = asyncio.create_task(adapter.sync_org_structure())
    await asyncio.wait_for(adapter.entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    ensure.assert_not_awaited()
    assert pool.raw.commits == 0
    assert adapter.writes == []


async def test_partial_fetch_commits_collected_data_but_skips_reconcile(sync_case):
    pool, provider, _, adapter, app = sync_case
    adapter.fail_fetch = True
    adapter.release.set()
    response = await post_sync(app, provider)
    assert response.status_code == 200
    assert response.json()["departments"] == 1
    assert response.json()["members"] == 0
    assert response.json()["errors"] == [
        "Fetch users in dept dept: remote failed",
        "Reconcile skipped due to partial sync failures",
    ]
    assert "reconcile" not in adapter.effects
    assert "counts" in adapter.effects
    assert pool.raw.commits == 1


@pytest.mark.parametrize("failure", ["write", "commit"])
async def test_failed_write_or_commit_rolls_back_before_mapping(monkeypatch, sync_case, failure):
    pool, provider, _, adapter, app = sync_case
    adapter.fail_write = failure == "write"
    if failure == "commit":
        monkeypatch.setattr(pool.raw, "commit", AsyncMock(side_effect=RuntimeError("commit failed")))
    adapter.release.set()
    response = await post_sync(app, provider)
    assert response.status_code == 200
    assert response.json()["errors"]
    assert pool.raw.commits == 0
    assert pool.raw.rollbacks == 1
    assert adapter.effects == []
    assert pool.active == 0
