"""SQL shape for tenant-disable bulk updates (no live Postgres)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Self
from uuid import UUID, uuid4

import pytest
from tests.membership_generation_fixtures import recording_user_writes

from app.core.domain.users import UserRole
from app.dao.agent_dao import agent_dao
from app.dao.schedule_dao import agent_schedule_dao
from app.dao.trigger_dao import agent_trigger_dao
from app.dao.user_dao import user_dao
from app.db import session as session_module
from app.db.connection import Params
from app.db.session import connection_ctx
from app.records.user import UserRecord


class _FakeCursor:
    def __init__(self, parent: _FakeRawConnection) -> None:
        self._parent = parent

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, query: str, params: Any = None) -> None:
        self._parent.executed.append((query, params))

    async def executemany(self, query: str, params_seq: Sequence[Params]) -> None:
        for params in params_seq:
            await self.execute(query, params)

    async def fetchone(self) -> dict[str, object]:
        _query, params = self._parent.executed[-1]
        return {"id": params["id"]}

    async def fetchall(self) -> list[dict[str, Any]]:
        query, _ = self._parent.executed[-1]
        if "FROM personal_burst_batches" in query:
            return []
        return [{"id": uuid4()}]


class _FakeRawConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    async def commit(self) -> None:
        return None

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


@pytest.fixture(autouse=True)
def _clear_ctx():
    token = session_module._conn_ctx.set(None)
    yield
    session_module._conn_ctx.reset(token)


@pytest.mark.asyncio
async def test_deactivate_for_tenant_skips_platform_admin(monkeypatch):
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    tenant_id = uuid4()
    await user_dao.deactivate_for_tenant(tenant_id)
    lock_query, lock_params = raw.executed[0]
    assert "FROM tenants" in lock_query
    assert "FOR UPDATE" in lock_query
    assert lock_params["id"] == tenant_id
    query, params = next((query, params) for query, params in raw.executed if query.lstrip().startswith("UPDATE users"))
    assert "role <> %(platform_admin)s" in query
    assert params["tenant_id"] == tenant_id
    assert params["platform_admin"] == "platform_admin"


@pytest.mark.asyncio
async def test_disable_agents_stops_not_paused(monkeypatch):
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    tenant_id = uuid4()
    await agent_dao.disable_for_tenant(tenant_id)
    lock_query, lock_params = raw.executed[0]
    assert "FROM agents" in lock_query
    assert "FOR UPDATE" in lock_query
    assert lock_params["tenant"] == tenant_id
    update_index = next(i for i, (query, _) in enumerate(raw.executed) if query.startswith("UPDATE agents"))
    assert any("UPDATE whatsapp_personal_connections" in query for query, _ in raw.executed[:update_index])
    query, params = raw.executed[update_index]
    assert "status = 'stopped'" in query
    assert "heartbeat_enabled = FALSE" in query
    assert "paused" not in query
    assert params["tenant_id"] == tenant_id


@pytest.mark.asyncio
async def test_disable_triggers_and_schedules_scope_to_tenant(monkeypatch):
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    tenant_id = uuid4()
    await agent_trigger_dao.disable_for_tenant(tenant_id)
    await agent_schedule_dao.disable_for_tenant(tenant_id)
    trigger_sql = raw.executed[0][0]
    schedule_sql = raw.executed[1][0]
    assert "FROM agents WHERE tenant_id = %(tenant_id)s" in trigger_sql
    assert "SET is_enabled = FALSE, self_disabled = FALSE" in trigger_sql
    assert "WHERE (is_enabled IS TRUE OR self_disabled IS TRUE) AND agent_id IN (" in trigger_sql
    assert "FROM agents WHERE tenant_id = %(tenant_id)s" in schedule_sql
    assert "next_run_at = NULL" in schedule_sql


@pytest.mark.parametrize("scoped", [False, True])
async def test_disable_creator_clears_provenance_in_scope(monkeypatch, scoped):
    # Given a creator and an optional tenant boundary.
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    creator_id, tenant_id = uuid4(), uuid4() if scoped else None
    # When disabling the creator's triggers.
    count = await agent_trigger_dao.disable_for_creator(creator_id, tenant_id=tenant_id)
    # Then enabled and self-disabled rows are cleared within that boundary.
    query, params = raw.executed[0]
    assert "SET is_enabled = FALSE, self_disabled = FALSE" in query
    assert "WHERE (is_enabled IS TRUE OR self_disabled IS TRUE) AND agent_id IN (" in query
    assert "FROM agents WHERE creator_id = %(creator_id)s" in query
    assert ("AND tenant_id = %(tenant_id)s" in query) is scoped
    assert params == ({"creator_id": creator_id, "tenant_id": tenant_id} if scoped else {"creator_id": creator_id})
    assert count == 1


_BUMP = "membership_generation = membership_generation + 1"
_GUARDED_BUMP = (
    "membership_generation = membership_generation + CASE WHEN is_active AND NOT %(is_active)s THEN 1 ELSE 0 END"
)


def _membership(*, tenant_id: UUID, role: UserRole = UserRole.MEMBER, is_active: bool = True) -> UserRecord:
    return UserRecord(id=uuid4(), identity_id=uuid4(), tenant_id=tenant_id, role=role, is_active=is_active)


async def test_tenant_disable_advances_the_generation_in_the_deactivating_update(monkeypatch):
    # Given an active member of a company.
    tenant_id = uuid4()
    member = _membership(tenant_id=tenant_id)
    with recording_user_writes(monkeypatch, member) as raw:
        # When the company is disabled.
        await user_dao.deactivate_for_tenant(tenant_id)
    # Then one UPDATE clears is_active and advances the generation, only for rows leaving the active state.
    [(query, _params)] = raw.updates_to("users")
    assert "SET is_active = FALSE" in query
    assert _BUMP in query
    assert "AND is_active IS TRUE" in query
    assert not raw.updates_to("identities")
    assert not any("auth_generation" in executed for executed, _ in raw.executed)


async def test_tenant_enable_leaves_the_generation_alone(monkeypatch):
    # Given a member deactivated with the company.
    tenant_id = uuid4()
    member = _membership(tenant_id=tenant_id, is_active=False)
    with recording_user_writes(monkeypatch, member) as raw:
        # When the company is enabled again.
        await user_dao.reactivate_for_tenant(tenant_id)
    # Then the activating UPDATE does not mention the generation, so earlier sessions stay revoked.
    [(query, _params)] = raw.updates_to("users")
    assert "SET is_active = TRUE" in query
    assert "membership_generation" not in query
    assert not raw.updates_to("identities")


async def test_deactivating_an_admin_with_a_peer_advances_the_generation_in_the_same_update(monkeypatch):
    # Given an org admin and the last-admin guard writer.
    admin = _membership(tenant_id=uuid4(), role=UserRole.ORG_ADMIN)
    with recording_user_writes(monkeypatch, admin) as raw:
        # When it deactivates that admin.
        updated = await user_dao.deactivate_unless_last_active(admin.id, role=admin.role, tenant_id=admin.tenant_id)
    # Then the guard's UPDATE clears is_active and advances the generation in one statement.
    assert updated is not None
    [(query, params)] = raw.updates_to("users")
    assert "SET is_active = FALSE" in query
    assert _BUMP in query
    assert "AND is_active IS TRUE" in query
    assert params["user_id"] == admin.id
    assert not any("auth_generation" in executed for executed, _ in raw.executed)


@pytest.mark.parametrize("is_active", [False, True], ids=["deactivate", "activate"])
async def test_generic_update_carries_the_guarded_advance_in_its_own_statement(monkeypatch, is_active):
    # Given a membership written through the generic update, as the activation and deactivation routes do.
    member = _membership(tenant_id=uuid4(), is_active=not is_active)
    with recording_user_writes(monkeypatch, member) as raw:
        async with connection_ctx():
            # When is_active is written.
            await user_dao.update(db_obj=member, obj_in={"is_active": is_active})
    # Then the same UPDATE decides the advance from the row's old state and the new value; there is no second write.
    [(query, params)] = raw.updates_to("users")
    assert "is_active = %(is_active)s" in query
    assert _GUARDED_BUMP in query
    assert params["is_active"] is is_active
    assert not raw.updates_to("identities")
    assert not any("auth_generation" in executed for executed, _ in raw.executed)


async def test_generic_update_without_is_active_never_mentions_the_generation(monkeypatch):
    # Given a membership edited without touching is_active.
    member = _membership(tenant_id=uuid4())
    with recording_user_writes(monkeypatch, member) as raw:
        # When the profile is written.
        await user_dao.update(db_obj=member, obj_in={"display_name": "Renamed"})
    # Then the statement leaves the generation alone.
    [(query, _params)] = raw.updates_to("users")
    assert "membership_generation" not in query


async def test_generation_is_read_from_sql_by_user_id(monkeypatch):
    # Given the generation column of one membership.
    member = _membership(tenant_id=uuid4())
    with recording_user_writes(monkeypatch, member) as raw:
        raw.generation = 4
        # When it is read for the bearer check.
        value = await user_dao.get_membership_generation(member.id)
    # Then it comes from the users table by primary key, never from a session snapshot.
    assert value == 4
    [(query, params)] = raw.executed
    assert query.startswith("SELECT membership_generation FROM users")
    assert params == {"user_id": member.id}
