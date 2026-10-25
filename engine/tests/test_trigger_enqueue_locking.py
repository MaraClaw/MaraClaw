"""Enqueue lock protocol tests using a strict raw psycopg fake (no Postgres)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Self, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from psycopg import AsyncConnection
from psycopg.errors import ForeignKeyViolation, LockNotAvailable, UniqueViolation
from psycopg.rows import DictRow

from app.dao.trigger_dao import trigger_execution_dao
from app.db import session as session_module
from app.db.connection import DbConnection
from app.db.errors import ForeignKeyViolationError, LockNotAvailableError
from app.records.trigger import AgentTriggerRecord

TRIGGER_LOCK = "SELECT 1 FROM agent_triggers WHERE id = %(trigger_id)s FOR KEY SHARE"
AGENT_LOCK = "SELECT 1 FROM agents WHERE id = %(agent_id)s FOR KEY SHARE"


class _Transaction:
    def __init__(self, raw: _Raw, force_rollback: bool) -> None:
        self.raw = raw
        self.force_rollback = force_rollback
        self.prior_locks: set[str] = set()

    async def __aenter__(self) -> Self:
        self.prior_locks = self.raw.locks.copy()
        self.raw.blocks.append(self)
        self.raw.events.append("begin rollback" if self.force_rollback else "begin")
        return self

    async def __aexit__(self, _kind: object, exc: BaseException | None, _tb: object) -> bool:
        assert self.raw.blocks.pop() is self
        rollback = self.force_rollback or exc is not None
        self.raw.events.append("rollback" if rollback else "commit")
        if rollback or not self.raw.blocks:
            self.raw.locks = self.prior_locks
        return False


class _Cursor:
    def __init__(self, raw: _Raw) -> None:
        self.raw = raw
        self.row: dict[str, object] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, query: str, params: dict[str, object]) -> None:
        sql = " ".join(query.split())
        raw = self.raw
        assert raw.blocks, "SQL must run in a transaction/savepoint"
        raw.events.append(sql)
        if sql == TRIGGER_LOCK:
            assert not raw.locks
            assert params["trigger_id"] == raw.data["trigger_id"]
            if raw.missing != "trigger":
                raw.locks.add("trigger")
            return
        if sql == AGENT_LOCK + " NOWAIT":
            assert params["agent_id"] == raw.data["agent_id"]
            if raw.conflicts:
                raw.conflicts -= 1
                raise LockNotAvailable("agent busy")
            if raw.missing != "agent":
                raw.locks.add("agent")
            return
        if sql == AGENT_LOCK:
            assert not raw.locks, "agent wait must not hold the trigger lock"
            assert raw.blocks[-1].force_rollback
            assert params["agent_id"] == raw.data["agent_id"]
            raw.locks.add("agent")
            return
        if sql.startswith("INSERT INTO trigger_executions (") and " RETURNING " in sql:
            assert raw.locks == {"trigger", "agent"} or raw.missing is not None
            if raw.missing is not None:
                raise ForeignKeyViolation("missing parent")
            if raw.duplicate:
                raise UniqueViolation("duplicate")
            self.row = {"lease_owner": None, "last_error": None, **params}
            return
        raise AssertionError(f"Unexpected SQL: {sql}")

    async def fetchone(self) -> dict[str, object] | None:
        return self.row


class _Raw:
    def __init__(self) -> None:
        self.data: dict[str, object] = {
            "id": uuid4(),
            "trigger_id": uuid4(),
            "agent_id": uuid4(),
            "source": "webhook",
            "idempotency_key": "delivery-1",
        }
        self.events: list[str] = []
        self.blocks: list[_Transaction] = []
        self.locks: set[str] = set()
        self.conflicts = 0
        self.duplicate = False
        self.missing: str | None = None

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self, *, force_rollback: bool = False) -> _Transaction:
        return _Transaction(self, force_rollback)


@pytest.fixture
def raw() -> Iterator[_Raw]:
    raw = _Raw()
    db = DbConnection(cast("AsyncConnection[DictRow]", raw))
    token = session_module._conn_ctx.set(db)
    try:
        yield raw
    finally:
        session_module._conn_ctx.reset(token)


@pytest.mark.asyncio
async def test_enqueue_locks_both_parents_before_insert(raw: _Raw) -> None:
    # Given / When
    record, created = await trigger_execution_dao.try_enqueue(obj_in=raw.data)
    # Then
    assert created
    assert record is not None
    assert record.id == raw.data["id"]
    assert raw.events[:3] == ["begin", TRIGGER_LOCK, AGENT_LOCK + " NOWAIT"]
    assert raw.events[3].startswith("INSERT INTO trigger_executions")
    assert raw.events[4:] == ["commit"]


@pytest.mark.asyncio
async def test_conflict_rolls_back_before_agent_only_wait_and_retry(raw: _Raw) -> None:
    # Given
    raw.conflicts = 1
    # When
    record, created = await trigger_execution_dao.try_enqueue(obj_in=raw.data)
    # Then
    assert created
    assert record is not None
    assert raw.events[:10] == [
        "begin",
        TRIGGER_LOCK,
        AGENT_LOCK + " NOWAIT",
        "rollback",
        "begin rollback",
        AGENT_LOCK,
        "rollback",
        "begin",
        TRIGGER_LOCK,
        AGENT_LOCK + " NOWAIT",
    ]
    assert raw.events[10].startswith("INSERT INTO trigger_executions")
    assert raw.events[11:] == ["commit"]
    assert not raw.locks
    assert not raw.blocks


@pytest.mark.asyncio
async def test_three_conflicts_raise_instead_of_duplicate(raw: _Raw) -> None:
    # Given
    raw.conflicts = 3
    # When / Then
    with pytest.raises(LockNotAvailableError) as caught:
        await trigger_execution_dao.try_enqueue(obj_in=raw.data)
    assert isinstance(caught.value.orig, LockNotAvailable)
    assert raw.events.count(AGENT_LOCK + " NOWAIT") == 3
    assert raw.events.count(AGENT_LOCK) == 2
    assert not raw.locks
    assert not raw.blocks


@pytest.mark.asyncio
async def test_unique_violation_still_returns_duplicate(raw: _Raw) -> None:
    # Given
    raw.duplicate = True
    # When
    result = await trigger_execution_dao.try_enqueue(obj_in=raw.data)
    # Then
    assert result == (None, False)
    assert raw.events[:3] == ["begin", TRIGGER_LOCK, AGENT_LOCK + " NOWAIT"]
    assert raw.events[-1] == "rollback"
    assert not raw.locks


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["trigger", "agent"])
async def test_missing_parent_reaches_insert_fk_violation(raw: _Raw, missing: str) -> None:
    # Given
    raw.missing = missing
    # When / Then
    with pytest.raises(ForeignKeyViolationError):
        await trigger_execution_dao.try_enqueue(obj_in=raw.data)
    assert raw.events[3].startswith("INSERT INTO trigger_executions")
    assert not raw.locks


@pytest.mark.asyncio
async def test_webhook_contention_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    from app.api import webhooks

    trigger = AgentTriggerRecord(id=uuid4(), agent_id=uuid4(), name="test", type="webhook")
    monkeypatch.setattr(webhooks, "_record_and_count_hits", AsyncMock(return_value=1))
    monkeypatch.setattr(webhooks.agent_trigger_dao, "find_webhook_by_token", AsyncMock(return_value=trigger))
    monkeypatch.setattr(webhooks.agent_dao, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(webhooks, "enqueue_webhook_execution", AsyncMock(side_effect=LockNotAvailableError("busy")))
    warning = []
    monkeypatch.setattr(webhooks.logger, "warning", lambda message: warning.append(message))
    app = FastAPI()
    app.include_router(webhooks.router)
    # When
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/webhooks/t/valid_token", json={"event": "test"})
    # Then
    assert response.status_code == 503
    assert response.json() == {"ok": False, "error": "busy"}
    assert len(warning) == 1
