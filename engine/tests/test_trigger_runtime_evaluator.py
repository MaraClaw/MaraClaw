import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import TracebackType
from typing import Any, Self, cast
from unittest.mock import AsyncMock

import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from app.core.json_types import JsonObject, JsonValue
from app.core.trigger_schedule import POLL_CHECKPOINT_KEY, next_due_at, poll_checkpoint
from app.dao.trigger_dao import agent_trigger_dao
from app.db import session as session_module
from app.db.connection import DbConnection
from app.records.trigger import AgentTriggerRecord
from app.services.trigger_runtime import dispatch, evaluator
from app.services.trigger_runtime.dispatch import enqueue_due_trigger, runtime_execution_payload
from app.services.trigger_runtime.evaluator import PollObservation, evaluate_trigger
from app.services.trigger_runtime.keys import (
    POLL_OBSERVED_KEY,
    build_okr_execution_key,
    build_scheduled_execution_key,
)
from app.services.trigger_runtime.okr import OKR_PERIOD_KEY, due_period, report_period

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


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

    async def fetchall(self) -> list[dict[str, object]]:
        return []


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
    [(sql, params), (reschedule, _)] = raw.executed
    assert reschedule.startswith("SELECT t.id")
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


def okr_trigger(name: str, **config: JsonValue) -> AgentTriggerRecord:
    return AgentTriggerRecord(uuid.uuid4(), uuid.uuid4(), name=name, type="cron", config=config, created_at=NOW)


@pytest.mark.parametrize(
    ("name", "today", "period"),
    [
        ("daily_okr_collection", date(2026, 11, 26), date(2026, 11, 26)),
        ("daily_okr_report", date(2026, 11, 26), date(2026, 11, 25)),
        ("daily_okr_report", date(2026, 3, 1), date(2026, 2, 28)),
        ("weekly_okr_report", date(2026, 11, 23), date(2026, 11, 16)),
        ("weekly_okr_report", date(2026, 11, 26), date(2026, 11, 16)),
        ("monthly_okr_report", date(2026, 11, 1), date(2026, 10, 31)),
        ("monthly_okr_report", date(2026, 1, 15), date(2025, 12, 31)),
    ],
)
def test_report_period_is_the_day_to_collect_or_the_generation_anchor(name: str, today: date, period: date) -> None:
    # Given a firing on a local day, When its period is derived, Then it is the one the report generator is called with.
    assert report_period(name, today) == period


@pytest.mark.parametrize(
    ("config", "agent_zone", "period"),
    [
        ({}, "Asia/Ho_Chi_Minh", date(2026, 11, 26)),
        ({}, "UTC", date(2026, 11, 25)),
        ({"timezone": "UTC"}, "Asia/Ho_Chi_Minh", date(2026, 11, 25)),
        ({"timezone": "Not/AZone"}, "Asia/Ho_Chi_Minh", date(2026, 11, 25)),
    ],
)
async def test_collection_period_is_the_local_day_in_the_schedules_zone(
    config: JsonObject, agent_zone: str, period: date
) -> None:
    # Given a collection firing at 18:30 UTC, which is already the next day in Ho Chi Minh
    trigger = okr_trigger("daily_okr_collection", **config)
    now = datetime(2026, 11, 25, 18, 30, tzinfo=UTC)
    # When the period is taken in the zone the schedule is evaluated in: its own, else its agent's, else UTC
    # Then neither the host's date nor UTC decide it
    assert await due_period(trigger, now, agent_zone) == period


async def test_period_resolves_the_agent_zone_itself_when_the_caller_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a trigger enqueued without a tick-resolved zone
    from app.services import timezone_utils

    zone = AsyncMock(return_value="Asia/Ho_Chi_Minh")
    monkeypatch.setattr(timezone_utils, "get_agent_timezone", zone)
    trigger = okr_trigger("daily_okr_collection")
    # When its period is taken
    period = await due_period(trigger, datetime(2026, 11, 25, 18, 30, tzinfo=UTC), None)
    # Then the agent's zone was looked up
    zone.assert_awaited_once_with(trigger.agent_id)
    assert period == date(2026, 11, 26)


async def test_okr_firing_is_enqueued_once_per_period_whichever_worker_or_tick_enqueues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a due weekly report and a recording enqueue
    trigger = okr_trigger("weekly_okr_report")
    queued: list[dict[str, Any]] = []

    async def enqueue(**kwargs: Any) -> tuple[None, bool]:
        queued.append(kwargs)
        return None, False

    monkeypatch.setattr(dispatch, "enqueue_trigger_execution", enqueue)
    # When two workers and a later tick enqueue it for the same week, and a tick a week later enqueues it again
    for now in (
        datetime(2026, 11, 23, 2, 0, tzinfo=UTC),
        datetime(2026, 11, 23, 2, 0, 15, tzinfo=UTC),
        datetime(2026, 11, 24, 1, 0, tzinfo=UTC),
        datetime(2026, 11, 30, 2, 0, tzinfo=UTC),
    ):
        await enqueue_due_trigger(trigger, now, "UTC")
    # Then the idempotency key is the trigger and the report period, and the period travels in the payload
    keys = [call["idempotency_key"] for call in queued]
    assert keys[:3] == [build_okr_execution_key(trigger, date(2026, 11, 16))] * 3
    assert keys[3] == build_okr_execution_key(trigger, date(2026, 11, 23))
    assert keys[0] != keys[3]
    assert [call["payload_obj"] for call in queued[:1]] == [{OKR_PERIOD_KEY: "2026-11-16"}]
    assert {call["source"] for call in queued} == {"cron"}


async def test_other_cron_triggers_keep_their_scheduled_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an ordinary cron trigger
    trigger = AgentTriggerRecord(
        uuid.uuid4(), uuid.uuid4(), name="digest", type="cron", config={"expr": "0 9 * * *"}, created_at=NOW
    )
    enqueue = AsyncMock(return_value=(None, True))
    monkeypatch.setattr(dispatch, "enqueue_trigger_execution", enqueue)
    # When it is enqueued
    await enqueue_due_trigger(trigger, NOW)
    # Then the key is the cron schedule's, not an OKR period key
    assert enqueue.await_args is not None
    assert enqueue.await_args.kwargs["idempotency_key"] == build_scheduled_execution_key(trigger, NOW)


def _scheduled(trigger_type: str, config: JsonObject, **columns: Any) -> AgentTriggerRecord:
    return AgentTriggerRecord(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name=trigger_type,
        type=trigger_type,
        config=config,
        created_at=NOW - timedelta(days=3, hours=5),
        **columns,
    )


@pytest.mark.parametrize(
    ("trigger", "zone"),
    [
        (_scheduled("cron", {"expr": "0 9 * * 1-5"}), "UTC"),
        (_scheduled("cron", {"expr": "*/20 * * * *"}), "Asia/Ho_Chi_Minh"),
        (_scheduled("cron", {"expr": "30 6 * * *", "timezone": "America/New_York"}), "UTC"),
        (
            _scheduled("cron", {"expr": "0 9 * * *"}, last_fired_at=NOW - timedelta(hours=30), cooldown_seconds=3600),
            "UTC",
        ),
        (_scheduled("interval", {"minutes": 90}), "UTC"),
        (_scheduled("interval", {"minutes": 1}, last_fired_at=NOW, cooldown_seconds=1800), "UTC"),
        (_scheduled("once", {"at": "2026-11-05T15:00:00"}), "UTC"),
        (
            _scheduled(
                "poll", {"url": "https://example.test", "interval_min": 1}, last_fired_at=NOW - timedelta(hours=1)
            ),
            "UTC",
        ),
        (
            _scheduled(
                "poll", {"url": "https://example.test", "interval_min": 30, POLL_CHECKPOINT_KEY: NOW.isoformat()}
            ),
            "UTC",
        ),
    ],
)
async def test_next_due_at_is_the_first_instant_the_evaluator_calls_the_trigger_due(
    trigger: AgentTriggerRecord, zone: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a stored trigger and its scheduled instant; evaluation reads no database beyond the poll claim
    checkpoints = _Checkpoints(monkeypatch)
    checkpoints.stored = poll_checkpoint(trigger.config)
    monkeypatch.setattr(evaluator, "poll_check", AsyncMock(return_value=PollObservation("x", 0, fire=True)))
    due = next_due_at(trigger, zone)
    assert due is not None
    # When evaluated just before and exactly at it
    before = await evaluate_trigger(
        replace(trigger, config=dict(trigger.config)), due - timedelta(seconds=1), agent_timezone=zone
    )
    at = await evaluate_trigger(replace(trigger, config=dict(trigger.config)), due, agent_timezone=zone)
    # Then the schedule and the evaluator agree
    assert (before, at) == (False, True)


@pytest.mark.parametrize(
    "trigger",
    [
        _scheduled("cron", {"expr": "not a cron"}),
        _scheduled("cron", {"expr": 5}),
        _scheduled("interval", {"minutes": "soon"}),
        _scheduled("once", {"at": "2026-11-05T15:00:00"}, fire_count=1),
        _scheduled("interval", {"minutes": 5}, max_fires=2, fire_count=2),
        _scheduled("interval", {"minutes": 5}, is_enabled=False),
        _scheduled("webhook", {"token": "t"}),
    ],
)
def test_a_trigger_that_can_never_be_due_has_no_schedule(trigger: AgentTriggerRecord) -> None:
    assert next_due_at(trigger, "UTC") is None


def test_expiry_makes_a_trigger_due_so_the_daemon_disables_it() -> None:
    trigger = _scheduled("interval", {"minutes": 600}, last_fired_at=NOW, expires_at=NOW + timedelta(minutes=5))
    assert next_due_at(trigger, "UTC") == NOW + timedelta(minutes=5)
