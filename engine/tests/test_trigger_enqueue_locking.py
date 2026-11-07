"""Enqueue lock protocol tests using a strict raw psycopg fake (no Postgres)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Self, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

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
from app.services.trigger_runtime import evaluator
from app.services.trigger_runtime.dispatch import enqueue_due_trigger
from app.services.trigger_runtime.evaluator import evaluate_trigger
from app.services.trigger_runtime.keys import POLL_CHECKPOINT_KEY, POLL_OBSERVED_KEY

NOW = datetime(2026, 11, 5, tzinfo=UTC)
TRIGGER_LOCK = "SELECT 1 FROM agent_triggers WHERE id = %(trigger_id)s FOR KEY SHARE"
AGENT_LOCK = "SELECT 1 FROM agents WHERE id = %(agent_id)s FOR KEY SHARE"


class _Transaction:
    def __init__(self, raw: _Raw, force_rollback: bool) -> None:
        self.raw = raw
        self.force_rollback = force_rollback
        self.prior_locks: set[str] = set()
        self.prior_keys: list[str] = []
        self.prior_config: dict[str, object] = {}

    async def __aenter__(self) -> Self:
        self.prior_locks = self.raw.locks.copy()
        self.prior_keys = self.raw.keys.copy()
        self.prior_config = self.raw.config.copy()
        self.raw.blocks.append(self)
        self.raw.events.append("begin rollback" if self.force_rollback else "begin")
        return self

    async def __aexit__(self, _kind: object, exc: BaseException | None, _tb: object) -> bool:
        assert self.raw.blocks.pop() is self
        rollback = self.force_rollback or exc is not None
        self.raw.events.append("rollback" if rollback else "commit")
        if rollback or not self.raw.blocks:
            self.raw.locks = self.prior_locks
        if rollback:
            self.raw.keys = self.prior_keys
            self.raw.config = self.prior_config
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
        assert raw.blocks or sql.startswith("UPDATE agent_triggers"), "SQL must run in a transaction/savepoint"
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
            if raw.duplicate or params["idempotency_key"] in raw.keys:
                raise UniqueViolation("duplicate")
            raw.keys.append(str(params["idempotency_key"]))
            self.row = {"lease_owner": None, "last_error": None, **params}
            return
        if sql.startswith("UPDATE agent_triggers SET config = config || jsonb_build_object('_poll_checked_at'"):
            assert set(params) == {"trigger_id", "seen", "expected_seq", "claimed_at"}
            if raw.checkpoint != params["seen"] or raw.config.get("_poll_seq", 0) != params["expected_seq"]:
                return
            raw.checkpoint = cast("str", params["claimed_at"])
            self.row = {"id": params["trigger_id"]}
            return
        if sql.startswith("UPDATE agent_triggers SET config = config || jsonb_build_object("):
            assert "'_last_value', %(value)s::text" in sql
            assert "'_poll_seq', %(expected_seq)s::bigint + 1" in sql
            assert "AND COALESCE((config->>'_poll_seq')::bigint, 0) = %(expected_seq)s RETURNING id" in sql
            assert set(params) == {"trigger_id", "expected_seq", "value"}
            raw.updates += 1
            current = raw.config.get("_poll_seq", 0)
            if current != params["expected_seq"]:
                return
            assert isinstance(params["expected_seq"], int)
            raw.config = {**raw.config, "_last_value": params["value"], "_poll_seq": params["expected_seq"] + 1}
            self.row = {"id": params["trigger_id"]}
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
        self.keys: list[str] = []
        self.config: dict[str, object] = {}
        self.checkpoint: str | None = None
        self.updates = 0
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


def _poll_trigger(raw: _Raw, observed: str) -> AgentTriggerRecord:
    """The trigger as the tick read it, carrying the observation `evaluate_trigger` hands over."""
    return AgentTriggerRecord(
        id=cast("UUID", raw.data["trigger_id"]),
        agent_id=cast("UUID", raw.data["agent_id"]),
        name="poll",
        type="poll",
        config={**raw.config, POLL_OBSERVED_KEY: observed},
    )


def _poll_key(raw: _Raw, sequence: int) -> str:
    return f"poll:{raw.data['trigger_id']}:{sequence}"


@pytest.mark.asyncio
async def test_poll_enqueue_advances_baseline_after_try_enqueue_in_one_transaction(raw: _Raw) -> None:
    # Given a trigger whose baseline is A at sequence 1
    raw.config = {"_last_value": "A", "_poll_seq": 1}
    # When the change to B is enqueued
    await enqueue_due_trigger(_poll_trigger(raw, "B"), NOW)
    # Then the enqueue keeps its own lock protocol and the conditional update follows it inside the outer transaction
    assert raw.events[:5] == ["begin", "begin", TRIGGER_LOCK, AGENT_LOCK + " NOWAIT", raw.events[4]]
    assert raw.events[4].startswith("INSERT INTO trigger_executions")
    assert raw.events[5] == "commit"
    assert raw.events[6].startswith("UPDATE agent_triggers SET config = config || jsonb_build_object(")
    assert raw.events[7:] == ["commit"]
    assert raw.keys == [_poll_key(raw, 2)]
    assert raw.config == {"_last_value": "B", "_poll_seq": 2}
    assert not raw.locks


@pytest.mark.asyncio
async def test_repeated_poll_transitions_a_b_a_b_make_three_executions(raw: _Raw) -> None:
    # Given a baseline of A
    raw.config = {"_last_value": "A", "_poll_seq": 1}
    # When the value flaps B, A, B
    for observed in ("B", "A", "B"):
        await enqueue_due_trigger(_poll_trigger(raw, observed), NOW)
    # Then every change is its own sequence-keyed execution, including the repeated B
    assert raw.keys == [_poll_key(raw, 2), _poll_key(raw, 3), _poll_key(raw, 4)]
    assert raw.config == {"_last_value": "B", "_poll_seq": 4}


@pytest.mark.asyncio
async def test_poll_enqueue_failure_keeps_baseline_and_next_poll_enqueues_the_change(raw: _Raw) -> None:
    # Given an enqueue that cannot take the agent lock
    raw.config = {"_last_value": "A", "_poll_seq": 1}
    raw.conflicts = 3
    # When the change to B fails to enqueue
    with pytest.raises(LockNotAvailableError):
        await enqueue_due_trigger(_poll_trigger(raw, "B"), NOW)
    # Then nothing moved and the update never ran
    assert raw.config == {"_last_value": "A", "_poll_seq": 1}
    assert raw.keys == []
    assert raw.updates == 0
    assert raw.events[-1] == "rollback"
    # When the next poll sees the same change
    await enqueue_due_trigger(_poll_trigger(raw, "B"), NOW)
    # Then it is enqueued and the baseline advances
    assert raw.keys == [_poll_key(raw, 2)]
    assert raw.config == {"_last_value": "B", "_poll_seq": 2}


@pytest.mark.asyncio
async def test_losing_the_conditional_update_rolls_back_the_execution(raw: _Raw) -> None:
    # Given a trigger read at sequence 1 while another worker has since adopted X at sequence 2
    raw.config = {"_last_value": "A", "_poll_seq": 1}
    stale = _poll_trigger(raw, "B")
    raw.config = {"_last_value": "X", "_poll_seq": 2}
    # When this worker enqueues its observation
    await enqueue_due_trigger(stale, NOW)
    # Then the update matched nothing, the execution is rolled back and the other baseline stands
    assert raw.updates == 1
    assert raw.keys == []
    assert raw.config == {"_last_value": "X", "_poll_seq": 2}
    assert raw.events[-1] == "rollback"


@pytest.mark.asyncio
async def test_legacy_row_without_sequence_starts_at_zero(raw: _Raw) -> None:
    # Given a row written before the sequence existed
    raw.config = {"_last_value": "A"}
    # When a change is enqueued
    await enqueue_due_trigger(_poll_trigger(raw, "B"), NOW)
    # Then the first execution is sequence 1
    assert raw.keys == [_poll_key(raw, 1)]
    assert raw.config == {"_last_value": "B", "_poll_seq": 1}


@pytest.mark.asyncio
async def test_existing_key_at_matching_sequence_still_advances_the_sequence(raw: _Raw) -> None:
    # Given a key left behind by a whole-config write that reset the sequence
    raw.config = {"_last_value": "A", "_poll_seq": 1}
    raw.keys = [_poll_key(raw, 2)]
    # When that sequence step is enqueued again
    await enqueue_due_trigger(_poll_trigger(raw, "B"), NOW)
    # Then no second execution is created, but the sequence moves on so later changes are not blocked
    assert raw.keys == [_poll_key(raw, 2)]
    assert raw.config == {"_last_value": "B", "_poll_seq": 2}


def _message_trigger(raw: _Raw, message_id: str) -> AgentTriggerRecord:
    return AgentTriggerRecord(
        id=cast("UUID", raw.data["trigger_id"]),
        agent_id=cast("UUID", raw.data["agent_id"]),
        name="inbox",
        type="on_message",
        config={"_matched_message": "deploy done", "_matched_message_id": message_id, "_matched_from": "Ray"},
    )


@pytest.mark.asyncio
async def test_identical_messages_make_two_executions_and_a_re_evaluated_message_none(raw: _Raw) -> None:
    # Given two chat messages with identical text but different ids
    first, second = _message_trigger(raw, "message-1"), _message_trigger(raw, "message-2")
    # When each is enqueued, and the first is evaluated again
    await enqueue_due_trigger(first, NOW)
    await enqueue_due_trigger(second, NOW)
    await enqueue_due_trigger(first, NOW)
    # Then there is one execution per message id and no baseline write
    trigger_id = raw.data["trigger_id"]
    assert raw.keys == [f"on_message:{trigger_id}:message-1", f"on_message:{trigger_id}:message-2"]
    assert raw.updates == 0


class _Source:
    """A polled endpoint whose JSON value the test moves between ticks."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.value = "A"
        self.now = NOW

        async def validate(url: str) -> str:
            return url

        def respond(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": self.value})

        def client(*, timeout: float, follow_redirects: bool) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=follow_redirects)

        monkeypatch.setattr(evaluator, "validate_public_url", validate)
        monkeypatch.setattr(evaluator, "public_http_client", client)

    async def tick(self, raw: _Raw, value: str, **options: object) -> None:
        """One daemon tick, an hour after the last: evaluate against the stored config, then enqueue when due."""
        self.value = value
        self.now += timedelta(hours=1)
        trigger = AgentTriggerRecord(
            id=cast("UUID", raw.data["trigger_id"]),
            agent_id=cast("UUID", raw.data["agent_id"]),
            name="status",
            type="poll",
            cooldown_seconds=0,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            config={
                "url": "https://status.example/api",
                "json_path": "$.status",
                **options,
                **raw.config,
                **({POLL_CHECKPOINT_KEY: raw.checkpoint} if raw.checkpoint is not None else {}),
            },
        )
        if await evaluate_trigger(trigger, self.now):
            await enqueue_due_trigger(trigger, self.now)


@pytest.mark.asyncio
async def test_first_poll_only_sets_the_baseline_then_every_flap_is_an_execution(
    raw: _Raw, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _Source(monkeypatch)
    # When the first poll sees A
    await source.tick(raw, "A")
    # Then it is a conditional baseline write and not an execution
    assert raw.keys == []
    assert raw.config == {"_last_value": "A", "_poll_seq": 1}
    # When the value then holds, flaps B, A, B
    for observed in ("A", "B", "A", "B"):
        await source.tick(raw, observed)
    # Then A, B, A, B yields three executions keyed by sequence
    assert raw.keys == [_poll_key(raw, 2), _poll_key(raw, 3), _poll_key(raw, 4)]
    assert raw.config == {"_last_value": "B", "_poll_seq": 4}


@pytest.mark.asyncio
async def test_match_mode_fires_each_time_the_value_enters_the_match_value(
    raw: _Raw, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _Source(monkeypatch)
    options = {"fire_on": "match", "match_value": "down"}
    # When the status goes up (baseline), down (enter), down (still matching), up (leave), down (re-enter)
    for observed in ("up", "down", "down", "up", "down"):
        await source.tick(raw, observed, **options)
    # Then the two entries fire and leaving only moves the baseline
    assert raw.keys == [_poll_key(raw, 2), _poll_key(raw, 4)]
    assert raw.config == {"_last_value": "down", "_poll_seq": 4}
