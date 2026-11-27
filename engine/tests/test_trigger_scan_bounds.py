import asyncio
import itertools
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from app.dao.trigger_dao import agent_trigger_dao
from app.records.trigger import AgentTriggerRecord, DueTrigger
from app.runtime.tasks import OwnedTasks
from app.services import trigger_daemon
from app.services.trigger_runtime.scan import Cursor, TriggerScanner

DUE_AT = datetime(2026, 1, 1, tzinfo=UTC)
ZONE = "Asia/Ho_Chi_Minh"

_ids = itertools.count(1)


def make_trigger(name: str) -> AgentTriggerRecord:
    return AgentTriggerRecord(
        id=uuid.UUID(int=next(_ids)), agent_id=uuid.uuid4(), name=name, type="interval", config={}, is_enabled=True
    )


class Table:
    """Due triggers, ordered by (due time, id), served through the DAO page method, recording every page request."""

    def __init__(self, triggers: list[AgentTriggerRecord]) -> None:
        self.rows: list[AgentTriggerRecord] = sorted(triggers, key=lambda trigger: trigger.id)
        self.requests: list[tuple[Cursor | None, int]] = []

    async def page(self, *, now: datetime, after: Cursor | None, limit: int) -> list[DueTrigger]:
        self.requests.append((after, limit))
        due = [DueTrigger(trigger, DUE_AT, ZONE) for trigger in self.rows]
        return [d for d in due if after is None or (d.due_at, d.trigger.id) > after][:limit]


@pytest.fixture(autouse=True)
async def isolated_daemon(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    async def claim_ready(
        _now: datetime, *, max_agents: int
    ) -> tuple[dict[uuid.UUID, list[AgentTriggerRecord]], set[uuid.UUID]]:
        return {}, set()

    monkeypatch.setattr(trigger_daemon, "claim_ready_trigger_invocations", claim_ready)
    monkeypatch.setattr(trigger_daemon, "_last_invoke", {})
    trigger_daemon.open_trigger_admission()
    try:
        yield
    finally:
        await trigger_daemon._scanner.close()
        trigger_daemon.open_trigger_admission()


def use_table(monkeypatch: pytest.MonkeyPatch, triggers: list[AgentTriggerRecord], *, limit: int = 8) -> Table:
    table = Table(triggers)
    monkeypatch.setattr(trigger_daemon.agent_trigger_dao, "list_due_page", table.page)
    monkeypatch.setattr(trigger_daemon, "_scanner", TriggerScanner(limit=limit))
    return table


def patch_evaluate(monkeypatch: pytest.MonkeyPatch, evaluate: Callable[[AgentTriggerRecord], Awaitable[bool]]) -> None:
    async def evaluate_trigger(trigger: AgentTriggerRecord, _now: datetime, _zone: str | None = None) -> bool:
        return await evaluate(trigger)

    monkeypatch.setattr(trigger_daemon, "_evaluate_trigger", evaluate_trigger)


async def settle_tasks() -> None:
    tasks = tuple(trigger_daemon._scanner._tasks.values())
    _ = await asyncio.gather(*tasks, return_exceptions=True)


async def test_ninth_trigger_is_evaluated_once_eight_hung_evaluations_hit_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given eight triggers whose evaluation never returns, then a ninth behind them, and a short deadline
    hung = [make_trigger(f"hung{i}") for i in range(8)]
    ninth = make_trigger("ninth")
    _ = use_table(monkeypatch, [*hung, ninth])
    monkeypatch.setattr(trigger_daemon, "EVALUATION_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(trigger_daemon, "EVALUATION_SETTLE_SECONDS", 0)
    started: list[str] = []
    ninth_done = asyncio.Event()

    async def evaluate(trigger: AgentTriggerRecord) -> bool:
        started.append(trigger.name)
        if trigger is ninth:
            ninth_done.set()
            return False
        await asyncio.Event().wait()
        return False

    patch_evaluate(monkeypatch, evaluate)
    # When a tick runs with every slot taken by hung evaluations
    await trigger_daemon._tick()
    assert started == [trigger.name for trigger in hung]
    # Then the ninth is deferred, not dropped, and the next tick cannot start it while all slots are held
    await trigger_daemon._tick()
    assert ninth.name not in started
    # And once the deadline frees the slots a later tick evaluates it
    await settle_tasks()
    await trigger_daemon._tick()
    await asyncio.wait_for(ninth_done.wait(), 1)
    assert started.count("ninth") == 1


async def test_deadline_warns_and_makes_the_trigger_eligible_again(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a trigger whose evaluation hangs
    hung = make_trigger("hung")
    _ = use_table(monkeypatch, [hung])
    monkeypatch.setattr(trigger_daemon, "EVALUATION_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(trigger_daemon, "EVALUATION_SETTLE_SECONDS", 0)
    warnings: list[str] = []
    monkeypatch.setattr(trigger_daemon.logger, "warning", lambda message, *_args: warnings.append(str(message)))
    attempts = 0

    async def evaluate(_trigger: AgentTriggerRecord) -> bool:
        nonlocal attempts
        attempts += 1
        await asyncio.Event().wait()
        return False

    patch_evaluate(monkeypatch, evaluate)
    # When the deadline passes and another tick runs
    await trigger_daemon._tick()
    await settle_tasks()
    await trigger_daemon._tick()
    await settle_tasks()
    # Then it logged a warning per timeout and was evaluated again
    assert attempts == 2
    assert [message for message in warnings if "hung" in message and "abandoned" in message] == warnings
    assert len(warnings) == 2


async def test_deadline_never_cancels_the_enqueue_after_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an evaluation that returns at once, and an enqueue that outlasts the deadline
    trigger = make_trigger("due")
    _ = use_table(monkeypatch, [trigger])
    monkeypatch.setattr(trigger_daemon, "EVALUATION_DEADLINE_SECONDS", 0.02)
    monkeypatch.setattr(trigger_daemon, "EVALUATION_SETTLE_SECONDS", 5)
    steps: list[str] = []

    async def evaluate(_trigger: AgentTriggerRecord) -> bool:
        return True

    async def enqueue(_trigger: AgentTriggerRecord, _now: datetime, _zone: str | None) -> None:
        await asyncio.sleep(0.1)
        steps.append("enqueue")

    patch_evaluate(monkeypatch, evaluate)
    monkeypatch.setattr(trigger_daemon, "enqueue_due_trigger", enqueue)
    # When the tick waits for it
    await trigger_daemon._tick()
    await settle_tasks()
    # Then the enqueue ran to completion
    assert steps == ["enqueue"]


@pytest.mark.parametrize(
    "name", ["daily_okr_collection", "daily_okr_report", "weekly_okr_report", "monthly_okr_report"]
)
async def test_due_okr_trigger_is_only_enqueued_with_the_resolved_zone(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # Given a due cron OKR trigger whose agent timezone the due page read with it
    trigger = AgentTriggerRecord(uuid.UUID(int=next(_ids)), uuid.uuid4(), name=name, type="cron", is_enabled=True)
    _ = use_table(monkeypatch, [trigger])
    monkeypatch.setattr(trigger_daemon, "EVALUATION_SETTLE_SECONDS", 5)

    async def evaluate(_trigger: AgentTriggerRecord) -> bool:
        return True

    enqueued: list[tuple[str, str | None]] = []

    async def enqueue(queued: AgentTriggerRecord, _now: datetime, zone: str | None) -> None:
        enqueued.append((queued.name, zone))

    patch_evaluate(monkeypatch, evaluate)
    monkeypatch.setattr(trigger_daemon, "enqueue_due_trigger", enqueue)
    # When the tick runs
    await trigger_daemon._tick()
    await settle_tasks()
    # Then it goes through the shared enqueue, in the zone the schedule is evaluated in, with no inline work
    assert enqueued == [(name, ZONE)]


async def test_population_larger_than_one_page_is_walked_in_bounded_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given twenty triggers and a bound of eight
    triggers = [make_trigger(f"t{i:02d}") for i in range(20)]
    table = use_table(monkeypatch, triggers)
    monkeypatch.setattr(trigger_daemon, "EVALUATION_SETTLE_SECONDS", 5)
    seen: list[str] = []
    active = peak = 0

    async def evaluate(trigger: AgentTriggerRecord) -> bool:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        seen.append(trigger.name)
        return False

    patch_evaluate(monkeypatch, evaluate)
    # When one tick runs
    await trigger_daemon._tick()
    # Then everything was evaluated exactly once in id order, no page exceeded the bound, and no more than
    # eight ran together
    assert seen == [trigger.name for trigger in table.rows]
    assert all(limit <= 8 for _after, limit in table.requests)
    assert len(table.requests) >= 3
    assert peak <= 8


async def test_cursor_persists_across_ticks_and_wraps_after_the_last_page(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given seven triggers, a bound of three and ticks that wait for nothing
    triggers = [make_trigger(f"t{i}") for i in range(7)]
    table = use_table(monkeypatch, triggers, limit=3)
    monkeypatch.setattr(trigger_daemon, "EVALUATION_SETTLE_SECONDS", 0)
    seen: list[str] = []

    async def evaluate(trigger: AgentTriggerRecord) -> bool:
        seen.append(trigger.name)
        return False

    patch_evaluate(monkeypatch, evaluate)
    # When ticks run, letting each tick's evaluations finish in between
    for _ in range(4):
        await trigger_daemon._tick()
        await settle_tasks()
    # Then each tick resumes after the last trigger the previous one started, and the fourth wraps to the start
    assert seen == ["t0", "t1", "t2", "t3", "t4", "t5", "t6", "t0", "t1", "t2"][: len(seen)]
    assert seen[:7] == [f"t{i}" for i in range(7)]
    assert seen[7:] == ["t0", "t1", "t2"]
    assert [after for after, _limit in table.requests[:3]] == [
        None,
        (DUE_AT, triggers[2].id),
        (DUE_AT, triggers[5].id),
    ]


async def test_hung_slots_never_starve_high_id_triggers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given two hung low-id triggers, a bound of three and four healthy triggers behind them
    hung = [make_trigger("hung0"), make_trigger("hung1")]
    healthy = [make_trigger(f"ok{i}") for i in range(4)]
    _ = use_table(monkeypatch, [*hung, *healthy], limit=3)
    monkeypatch.setattr(trigger_daemon, "EVALUATION_SETTLE_SECONDS", 0)
    seen: list[str] = []

    async def evaluate(trigger: AgentTriggerRecord) -> bool:
        seen.append(trigger.name)
        if trigger in hung:
            await asyncio.Event().wait()
        return False

    patch_evaluate(monkeypatch, evaluate)
    # When ticks run while the two stay hung
    for _ in range(6):
        await trigger_daemon._tick()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    # Then the single free slot still rotates through every healthy trigger, and the hung ones ran once
    assert {name for name in seen if name.startswith("ok")} == {f"ok{i}" for i in range(4)}
    assert seen.count("hung0") == seen.count("hung1") == 1


async def test_close_cancels_and_awaits_tasks_and_resets_the_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a scanner holding a running evaluation after a partial walk
    triggers = [make_trigger(f"t{i}") for i in range(4)]
    _ = use_table(monkeypatch, triggers, limit=2)
    monkeypatch.setattr(trigger_daemon, "EVALUATION_SETTLE_SECONDS", 0)
    finalized: list[str] = []

    async def evaluate(trigger: AgentTriggerRecord) -> bool:
        try:
            await asyncio.Event().wait()
        finally:
            finalized.append(trigger.name)
        return False

    patch_evaluate(monkeypatch, evaluate)
    await trigger_daemon._tick()
    await asyncio.sleep(0)
    # When it closes
    await trigger_daemon._scanner.close()
    # Then the evaluations were cancelled and finished first, and the walk restarts from the beginning
    assert sorted(finalized) == ["t0", "t1"]
    assert not trigger_daemon._scanner._tasks
    assert trigger_daemon._scanner._cursor is None


async def test_dao_due_page_is_keyset_ordered_by_due_time_then_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a database session that records the statement
    statements: list[tuple[str, dict[str, object]]] = []

    class Db:
        async def fetchall(self, query: str, params: dict[str, object]) -> list[dict[str, object]]:
            statements.append((" ".join(query.split()), params))
            return []

    @asynccontextmanager
    async def session() -> AsyncGenerator[Db]:
        yield Db()

    monkeypatch.setattr(agent_trigger_dao, "session", session)
    after = (DUE_AT, uuid.uuid4())
    # When the first and a following page are read
    assert await agent_trigger_dao.list_due_page(now=DUE_AT, after=None, limit=5) == []
    assert await agent_trigger_dao.list_due_page(now=DUE_AT, after=after, limit=7) == []
    # Then both read only due, enabled rows in index order and are bounded, and only the second filters past the cursor
    first, second = statements
    due_filter = "t.is_enabled IS TRUE AND t.next_due_at IS NOT NULL AND t.next_due_at <= %(now)s"
    assert due_filter in first[0]
    assert due_filter in second[0]
    assert first[0].endswith("ORDER BY t.next_due_at, t.id LIMIT %(limit)s")
    assert "(t.next_due_at, t.id) > (%(after_due)s, %(after_id)s)" not in first[0]
    assert second[0].endswith(
        "(t.next_due_at, t.id) > (%(after_due)s, %(after_id)s) ORDER BY t.next_due_at, t.id LIMIT %(limit)s"
    )
    assert first[1] == {"now": DUE_AT, "limit": 5}
    assert second[1] == {"now": DUE_AT, "limit": 7, "after_due": after[0], "after_id": after[1]}


class FakeClock:
    def __init__(self) -> None:
        self.now: float = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_loop_beats_only_after_a_tick_finishes_so_a_stuck_tick_fails_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the daemon loop on a budgeted owned task whose second tick hangs in an inline await
    clock = FakeClock()
    tasks = OwnedTasks(clock)
    first_done, stuck = asyncio.Event(), asyncio.Event()
    release = asyncio.Event()
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            first_done.set()
            return
        if ticks == 2:
            stuck.set()
            await release.wait()

    monkeypatch.setattr(trigger_daemon, "_tick", tick)
    monkeypatch.setattr(trigger_daemon, "TICK_INTERVAL", 0)
    tasks.start(
        "trigger_daemon",
        trigger_daemon.start_trigger_daemon(),
        requirement="loop",
        progress_budget_seconds=trigger_daemon.PROGRESS_BUDGET_SECONDS,
    )
    await asyncio.wait_for(stuck.wait(), 1)
    # When the budget passes while the tick is stuck
    clock.now += trigger_daemon.PROGRESS_BUDGET_SECONDS + 1
    # Then readiness fails, but the loop is not cancelled
    assert not tasks.ready()
    assert not tasks.required["trigger_daemon"][0].done()
    # And a tick that finishes beats again
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert tasks.ready()
    await tasks.stop()


async def test_failed_tick_does_not_beat(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a loop whose ticks keep raising
    clock = FakeClock()
    tasks = OwnedTasks(clock)
    attempted = asyncio.Event()

    async def tick() -> None:
        attempted.set()
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(trigger_daemon, "_tick", tick)
    monkeypatch.setattr(trigger_daemon, "TICK_INTERVAL", 3600)
    tasks.start("trigger_daemon", trigger_daemon.start_trigger_daemon(), requirement="loop", progress_budget_seconds=30)
    await asyncio.wait_for(attempted.wait(), 1)
    await asyncio.sleep(0)
    # When the budget passes
    clock.now += 31
    # Then the failure was not mistaken for progress
    assert not tasks.ready()
    await tasks.stop()
