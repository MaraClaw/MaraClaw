import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self, cast
from unittest.mock import AsyncMock

import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from app.dao.trigger_dao import agent_trigger_dao
from app.db import session as session_module
from app.db.connection import DbConnection
from app.records.trigger import AgentTriggerRecord
from app.services.trigger_runtime import evaluator
from app.services.trigger_runtime.dispatch import runtime_execution_payload
from app.services.trigger_runtime.evaluator import PollObservation, evaluate_trigger
from app.services.trigger_runtime.keys import (
    POLL_CHECKPOINT_KEY,
    POLL_OBSERVED_KEY,
    build_scheduled_execution_key,
    poll_checkpoint,
)

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


async def test_mark_fired_records_only_a_self_disable_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db import session as session_module
    from app.services.trigger_runtime.evaluator import mark_trigger_fired
    from test_tenant_disable_dao import _FakePool, _FakeRawConnection

    # Given a recording connection, without a live database.
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    token = session_module._conn_ctx.set(None)
    trigger_id, now = uuid.uuid4(), datetime.now(UTC)
    # When recording a firing.
    try:
        await mark_trigger_fired(trigger_id, now)
    finally:
        session_module._conn_ctx.reset(token)
    # Then only an enabled -> disabled transition records self provenance.
    query, params = raw.executed[0]
    assert (
        "self_disabled = CASE WHEN is_enabled IS TRUE AND (type = 'once' OR "
        "(max_fires IS NOT NULL AND fire_count + 1 >= max_fires)) THEN TRUE ELSE self_disabled END"
    ) in " ".join(query.split())
    assert params == {"id": trigger_id, "now": now}


@pytest.mark.asyncio
async def test_evaluate_trigger_returns_false_for_non_numeric_interval() -> None:
    # Given an interval trigger with an invalid persisted interval value.
    now = datetime.now(UTC)
    trigger = AgentTriggerRecord(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name="invalid-interval",
        reason="test",
        is_enabled=True,
        expires_at=None,
        max_fires=None,
        fire_count=0,
        last_fired_at=None,
        cooldown_seconds=0,
        config={"minutes": "ten"},
        type="interval",
        created_at=now - timedelta(hours=1),
    )

    # When the trigger is evaluated.
    result = await evaluate_trigger(trigger, now)

    # Then it is not due rather than raising from the persisted value.
    assert result is False


def _poll_trigger(config: dict[str, Any]) -> AgentTriggerRecord:
    return AgentTriggerRecord(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name="status-page",
        type="poll",
        reason="watch",
        cooldown_seconds=0,
        config={"url": "https://status.example/api", **config},
        created_at=NOW - timedelta(hours=1),
    )


class _Checkpoints:
    """The checkpoint column of one trigger row, claimed with the same compare-and-set as the conditional UPDATE."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, stored: str | None = None) -> None:
        self.stored: str | None = stored
        self.sequence: int | None = None
        self.claims: list[bool] = []
        monkeypatch.setattr(agent_trigger_dao, "claim_poll_checkpoint", self.claim)

    async def claim(self, _trigger_id: uuid.UUID, *, seen: str | None, expected_seq: int, claimed_at: datetime) -> bool:
        won = self.stored == seen and (self.sequence is None or self.sequence == expected_seq)
        if won:
            self.stored = claimed_at.isoformat()
        self.claims.append(won)
        return won


async def test_baseline_only_observation_is_a_conditional_update_and_not_an_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a first poll that only establishes the baseline
    _ = _Checkpoints(monkeypatch)
    trigger = _poll_trigger({"_poll_seq": 3})
    advance = AsyncMock(return_value=True)
    monkeypatch.setattr(evaluator, "poll_check", AsyncMock(return_value=PollObservation("up", 3, fire=False)))
    monkeypatch.setattr(agent_trigger_dao, "advance_poll_baseline", advance)
    # When the trigger is evaluated
    due = await evaluate_trigger(trigger, NOW)
    # Then nothing is due and the baseline moved only against the sequence that was read
    assert due is False
    advance.assert_awaited_once_with(trigger.id, expected_seq=3, value="up")
    assert POLL_OBSERVED_KEY not in trigger.config


async def test_firing_observation_is_handed_to_the_dispatcher_without_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a change that fires
    _ = _Checkpoints(monkeypatch)
    trigger = _poll_trigger({"_last_value": "up", "_poll_seq": 3})
    advance = AsyncMock()
    monkeypatch.setattr(evaluator, "poll_check", AsyncMock(return_value=PollObservation("down", 3, fire=True)))
    monkeypatch.setattr(agent_trigger_dao, "advance_poll_baseline", advance)
    # When the trigger is evaluated
    due = await evaluate_trigger(trigger, NOW)
    # Then it is due, the observation rides on the in-memory trigger, and the baseline is untouched in the database
    assert due is True
    assert trigger.config[POLL_OBSERVED_KEY] == "down"
    assert trigger.config["_last_value"] == "up"
    advance.assert_not_awaited()


async def test_unchanged_poll_is_not_due(monkeypatch: pytest.MonkeyPatch) -> None:
    _ = _Checkpoints(monkeypatch)
    advance = AsyncMock()
    monkeypatch.setattr(evaluator, "poll_check", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_trigger_dao, "advance_poll_baseline", advance)

    assert await evaluate_trigger(_poll_trigger({"_last_value": "up"}), NOW) is False
    advance.assert_not_awaited()


@pytest.mark.parametrize(
    ("config", "sequence"),
    [({}, 1), ({"_poll_seq": 0}, 1), ({"_poll_seq": 6}, 7), ({"_poll_seq": "junk"}, 1), ({"_poll_seq": True}, 1)],
)
def test_poll_key_is_the_next_sequence_not_a_hash_of_the_value(config: dict[str, Any], sequence: int) -> None:
    # Given a poll trigger whose baseline value is irrelevant to the key
    trigger = _poll_trigger({"_last_value": "B", **config})
    # When / Then the key is the sequence the execution advances to
    assert build_scheduled_execution_key(trigger, NOW) == f"poll:{trigger.id}:{sequence}"


def test_poll_keys_differ_for_a_repeated_value() -> None:
    # Given A -> B and the later B -> A -> B: the same observed value at different sequences
    trigger = _poll_trigger({"_last_value": "A", "_poll_seq": 1})
    first = build_scheduled_execution_key(trigger, NOW)
    trigger.config["_poll_seq"] = 3
    assert build_scheduled_execution_key(trigger, NOW) != first


def _message_trigger(message_id: str | None, text: str = "deploy done") -> AgentTriggerRecord:
    config: dict[str, Any] = {"_matched_message": text, "_matched_from": "Ray"}
    if message_id is not None:
        config["_matched_message_id"] = message_id
    return AgentTriggerRecord(id=uuid.uuid4(), agent_id=uuid.uuid4(), name="inbox", type="on_message", config=config)


def test_on_message_key_is_the_chat_message_id() -> None:
    first, same_text = _message_trigger("m-1"), _message_trigger("m-2")
    same_text.id = first.id
    assert build_scheduled_execution_key(first, NOW) == f"on_message:{first.id}:m-1"
    # Identical text under another message id is another execution; the same id is the same one.
    assert build_scheduled_execution_key(same_text, NOW) != build_scheduled_execution_key(first, NOW)
    assert build_scheduled_execution_key(first, NOW) == build_scheduled_execution_key(first, NOW)


def test_on_message_key_requires_the_message_id() -> None:
    with pytest.raises(ValueError, match="chat message id"):
        build_scheduled_execution_key(_message_trigger(None), NOW)


def test_execution_payload_carries_the_matched_message_id() -> None:
    payload = runtime_execution_payload(_message_trigger("m-9"))
    assert payload == {"_matched_message": "deploy done", "_matched_message_id": "m-9", "_matched_from": "Ray"}
    assert POLL_OBSERVED_KEY not in runtime_execution_payload(_poll_trigger({POLL_OBSERVED_KEY: "x"}))


class _Cursor:
    def __init__(self, raw: _Raw) -> None:
        self.raw = raw

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, _kind: type[BaseException] | None, _exc: BaseException | None, _tb: TracebackType | None
    ) -> bool:
        return False

    async def execute(self, query: str, params: dict[str, object]) -> None:
        self.raw.executed.append((" ".join(query.split()), params))

    async def fetchone(self) -> dict[str, object] | None:
        return {"id": self.raw.executed[-1][1]["trigger_id"]} if self.raw.matches else None


class _Raw:
    def __init__(self, *, matches: bool) -> None:
        self.matches = matches
        self.executed: list[tuple[str, dict[str, object]]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


@pytest.mark.parametrize("matches", [True, False])
async def test_advance_poll_baseline_updates_only_internal_keys_conditionally(matches: bool) -> None:
    # Given a connection whose conditional update matches or not
    raw = _Raw(matches=matches)
    token = session_module._conn_ctx.set(DbConnection(cast("AsyncConnection[DictRow]", raw)))
    trigger_id = uuid.uuid4()
    try:
        # When the baseline is advanced from sequence 4
        advanced = await agent_trigger_dao.advance_poll_baseline(trigger_id, expected_seq=4, value="down")
    finally:
        session_module._conn_ctx.reset(token)
    # Then the statement merges two internal keys into the row's own config and compares on the sequence
    assert advanced is matches
    [(sql, params)] = raw.executed
    assert sql == (
        "UPDATE agent_triggers SET config = config || jsonb_build_object( '_last_value', %(value)s::text, "
        "'_poll_seq', %(expected_seq)s::bigint + 1 ) WHERE id = %(trigger_id)s "
        "AND COALESCE((config->>'_poll_seq')::bigint, 0) = %(expected_seq)s RETURNING id"
    )
    assert params == {"trigger_id": trigger_id, "expected_seq": 4, "value": "down"}


async def test_unchanged_poll_waits_for_the_next_interval_instead_of_polling_every_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a trigger whose first interval passed and whose poll saw nothing new
    checkpoints = _Checkpoints(monkeypatch)
    poll = AsyncMock(return_value=None)
    monkeypatch.setattr(evaluator, "poll_check", poll)
    trigger = _poll_trigger({"interval_min": 10, "_last_value": "up"})
    assert await evaluate_trigger(trigger, NOW) is False
    # When later ticks arrive, as the next worker snapshot carries the checkpoint
    trigger.config[POLL_CHECKPOINT_KEY] = checkpoints.stored
    for seconds in (15, 30, 9 * 60 + 45):
        assert await evaluate_trigger(trigger, NOW + timedelta(seconds=seconds)) is False
    # Then no request was made until a full interval passed
    assert poll.await_count == 1
    assert await evaluate_trigger(trigger, NOW + timedelta(minutes=10)) is False
    assert poll.await_count == 2
    assert checkpoints.claims == [True, True]


async def test_failed_poll_waits_for_the_next_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a poll request that fails after the claim
    checkpoints = _Checkpoints(monkeypatch)
    advance = AsyncMock()
    monkeypatch.setattr(agent_trigger_dao, "advance_poll_baseline", advance)

    async def unreachable(_url: str) -> str:
        raise TimeoutError

    monkeypatch.setattr(evaluator, "validate_public_url", unreachable)
    trigger = _poll_trigger({"interval_min": 10, "_last_value": "up"})
    # When it is evaluated, then again a tick later
    assert await evaluate_trigger(trigger, NOW) is False
    trigger.config[POLL_CHECKPOINT_KEY] = checkpoints.stored
    assert await evaluate_trigger(trigger, NOW + timedelta(seconds=15)) is False
    # Then the failure consumed the interval: one claim, no baseline write
    assert checkpoints.claims == [True]
    advance.assert_not_awaited()


async def test_shortened_interval_applies_to_the_stored_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a poll claimed at NOW under an hour-long interval
    checkpoints = _Checkpoints(monkeypatch)
    poll = AsyncMock(return_value=None)
    monkeypatch.setattr(evaluator, "poll_check", poll)
    trigger = _poll_trigger({"interval_min": 60, "_last_value": "up"})
    assert await evaluate_trigger(trigger, NOW) is False
    trigger.config[POLL_CHECKPOINT_KEY] = checkpoints.stored
    assert await evaluate_trigger(trigger, NOW + timedelta(minutes=6)) is False
    assert poll.await_count == 1
    # When the interval is shortened to the minimum
    trigger.config["interval_min"] = 5
    # Then the next poll is due 5 minutes after the checkpoint, not after the hour
    assert await evaluate_trigger(trigger, NOW + timedelta(minutes=6)) is False
    assert poll.await_count == 2


async def test_interval_counts_from_a_later_firing_than_the_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a firing recorded after the last claimed poll
    checkpoints = _Checkpoints(monkeypatch, stored=NOW.isoformat())
    poll = AsyncMock(return_value=None)
    monkeypatch.setattr(evaluator, "poll_check", poll)
    trigger = _poll_trigger({POLL_CHECKPOINT_KEY: NOW.isoformat()})
    trigger.last_fired_at = NOW + timedelta(minutes=4)
    # When / Then the interval measures from the firing
    assert await evaluate_trigger(trigger, NOW + timedelta(minutes=8)) is False
    assert poll.await_count == 0
    assert await evaluate_trigger(trigger, NOW + timedelta(minutes=9)) is False
    assert poll.await_count == 1
    assert checkpoints.claims == [True]


async def test_url_edit_clears_the_checkpoint_so_the_next_tick_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a recently polled trigger and a user edit that drops the checkpoint, as merge_user_config does
    checkpoints = _Checkpoints(monkeypatch, stored=NOW.isoformat())
    poll = AsyncMock(return_value=None)
    monkeypatch.setattr(evaluator, "poll_check", poll)
    trigger = _poll_trigger({POLL_CHECKPOINT_KEY: NOW.isoformat(), "_last_value": "up"})
    trigger.created_at = NOW - timedelta(days=1)
    soon = NOW + timedelta(seconds=15)
    assert await evaluate_trigger(trigger, soon) is False
    assert poll.await_count == 0
    del trigger.config[POLL_CHECKPOINT_KEY]
    checkpoints.stored = None
    # When the next tick evaluates the edited trigger
    assert await evaluate_trigger(trigger, soon) is False
    # Then it polls at once
    assert poll.await_count == 1


async def test_two_racing_evaluations_make_one_request_per_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given two replicas that read the same trigger row and race to claim it
    checkpoints = _Checkpoints(monkeypatch)
    poll = AsyncMock(return_value=PollObservation("down", 0, fire=True))
    monkeypatch.setattr(evaluator, "poll_check", poll)
    first = _poll_trigger({"_last_value": "up"})
    second = replace(first, config=dict(first.config))
    # When both evaluate concurrently
    results = await asyncio.gather(evaluate_trigger(first, NOW), evaluate_trigger(second, NOW))
    # Then exactly one claim won, one request was made, and only the winner is due
    assert sorted(checkpoints.claims) == [False, True]
    assert poll.await_count == 1
    assert sorted(results) == [False, True]


async def test_stale_source_cannot_claim_after_config_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    checkpoints = _Checkpoints(monkeypatch)
    stale = _poll_trigger({})
    checkpoints.sequence = 1
    poll = AsyncMock()
    monkeypatch.setattr(evaluator, "poll_check", poll)
    assert not await evaluate_trigger(stale, NOW)
    poll.assert_not_awaited()


@pytest.mark.parametrize("stored", [None, "2026-11-05T12:00:00+00:00"])
async def test_claim_poll_checkpoint_is_a_conditional_update_of_one_internal_key(stored: str | None) -> None:
    # Given a connection whose conditional update matches
    raw = _Raw(matches=True)
    token = session_module._conn_ctx.set(DbConnection(cast("AsyncConnection[DictRow]", raw)))
    trigger_id = uuid.uuid4()
    try:
        # When the checkpoint is claimed against the value the caller saw
        won = await agent_trigger_dao.claim_poll_checkpoint(trigger_id, seen=stored, expected_seq=2, claimed_at=NOW)
    finally:
        session_module._conn_ctx.reset(token)
    # Then only the checkpoint key is written, for an enabled row whose checkpoint is unchanged
    assert won is True
    [(sql, params)] = raw.executed
    assert sql.startswith("UPDATE agent_triggers SET config = config || jsonb_build_object('_poll_checked_at', ")
    assert "COALESCE((config->>'_poll_seq')::bigint, 0) = %(expected_seq)s" in sql
    assert "'_last_value'" not in sql
    assert "last_fired_at" not in sql
    assert "AND is_enabled IS TRUE" in sql
    assert "IS NOT DISTINCT FROM %(seen)s::text RETURNING id" in sql
    assert params == {"trigger_id": trigger_id, "seen": stored, "expected_seq": 2, "claimed_at": NOW.isoformat()}


def test_checkpoint_that_is_not_a_string_counts_as_absent() -> None:
    assert poll_checkpoint({POLL_CHECKPOINT_KEY: 5}) is None
    assert poll_checkpoint({}) is None
    assert poll_checkpoint({POLL_CHECKPOINT_KEY: "x"}) == "x"
