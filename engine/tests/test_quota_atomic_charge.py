from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib import import_module
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.dao.user_dao import user_dao
from app.db.connection import DbConnection
from app.records.user import UserRecord
from app.services import quota_guard


@pytest.mark.parametrize("returned", [None, 50])
async def test_charge_uses_one_predicated_update_without_cache_bump(
    monkeypatch: pytest.MonkeyPatch, returned: int | None
):
    statements: list[tuple[str, object]] = []

    async def fetchone(_db: DbConnection, query: str, params: object = None):
        statements.append((query, params))
        return {"quota_messages_used": returned} if returned is not None else None

    @asynccontextmanager
    async def fake_session() -> AsyncGenerator[DbConnection]:
        yield DbConnection(AsyncMock())

    monkeypatch.setattr(user_dao, "session", fake_session)
    monkeypatch.setattr(DbConnection, "fetchone", fetchone)
    bump = AsyncMock()
    monkeypatch.setattr(import_module("app.dao.user_dao"), "bump_user_session", bump)
    user_id = uuid4()

    result = await user_dao.charge_message_quota(user_id)

    assert result == returned
    assert len(statements) == 1
    query, params = statements[0]
    sql = " ".join(query.split())
    assert sql.startswith("UPDATE users SET")
    assert "SELECT" not in sql
    assert "WITH " not in sql
    assert "WHERE id = %(id)s AND CASE WHEN" in sql
    assert "END < quota_message_limit RETURNING quota_messages_used" in sql
    assert "THEN 0 ELSE quota_messages_used END + 1" in sql
    assert params == {"id": user_id}
    bump.assert_not_awaited()


@pytest.mark.parametrize(("period", "days"), [("daily", 1), ("weekly", 7), ("monthly", 30)])
async def test_charge_folds_period_and_null_start_reset_into_update(
    monkeypatch: pytest.MonkeyPatch, period: str, days: int
):
    captured: list[str] = []

    async def fetchone(_db: DbConnection, query: str, _params: object):
        captured.append(query)
        return {"quota_messages_used": 1}

    @asynccontextmanager
    async def fake_session() -> AsyncGenerator[DbConnection]:
        yield DbConnection(AsyncMock())

    monkeypatch.setattr(user_dao, "session", fake_session)
    monkeypatch.setattr(DbConnection, "fetchone", fetchone)

    assert await user_dao.charge_message_quota(uuid4()) == 1

    sql = " ".join(captured[0].split())
    assert f"WHEN '{period}' THEN interval '{days} day{'s' if days != 1 else ''}'" in sql
    assert sql.count("quota_message_period <> 'permanent'") == 3
    assert sql.count("quota_period_start IS NULL") == 3
    assert "THEN now() ELSE quota_period_start END" in sql


@pytest.mark.parametrize("role", ["platform_admin", "org_admin"])
async def test_fresh_admin_principal_is_exempt_without_dao_reads(monkeypatch: pytest.MonkeyPatch, role: str):
    charge, read = AsyncMock(), AsyncMock()
    monkeypatch.setattr(user_dao, "charge_message_quota", charge)
    monkeypatch.setattr(user_dao, "get", read)

    await quota_guard.charge_conversation_quota(UserRecord(id=uuid4(), role=role, quota_message_limit=0))

    charge.assert_not_awaited()
    read.assert_not_awaited()


async def test_admission_at_limit_rejects_without_read_then_write(monkeypatch: pytest.MonkeyPatch):
    user = UserRecord(id=uuid4(), quota_message_limit=3, quota_messages_used=2)
    charge = AsyncMock(side_effect=[3, None])
    read = AsyncMock(return_value=UserRecord(id=user.id, quota_message_limit=3, quota_messages_used=3))
    monkeypatch.setattr(user_dao, "charge_message_quota", charge)
    monkeypatch.setattr(user_dao, "get", read)

    await quota_guard.charge_conversation_quota(user)
    read.assert_not_awaited()
    with pytest.raises(quota_guard.QuotaExceeded) as error:
        await quota_guard.charge_conversation_quota(user)

    assert error.value.quota_type == "conversation"
    assert "(3/3)" in error.value.message
    assert charge.await_count == 2
    read.assert_awaited_once_with(user.id)
