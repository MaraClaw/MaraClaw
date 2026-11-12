"""Idle-tick cost and forced rereads of the delayed-reply sweep, over real transcript files and counting fakes."""

import asyncio
from collections.abc import AsyncGenerator, Iterator, Sequence
from contextlib import AbstractContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from sqlite3 import Connection
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.db.errors import DbError
from app.records.chat import ChatMessageRecord
from app.records.openclaw_reply_binding import OpenClawReplyBinding
from app.services import openclaw_delayed_replies as recovery, openclaw_reply_authorization as authorization
from app.services.openclaw_transcript import (
    TranscriptQuery,
    TranscriptReply,
    TranscriptRow,
    TranscriptSource,
    open_transcript,
)
from app.services.quota_guard import AgentExpired
from app.services.response_identity import ResponseGuard
from test_openclaw_delayed_replies import append, transcript
from worker_progress_fixtures import WorkerHTTP, worker_http

__all__ = ["worker_http"]

OWNER, USER, TENANT = uuid4(), uuid4(), uuid4()


@dataclass(slots=True)
class Counters:
    locks: int = 0
    access: int = 0
    checkpoints: list[dict[str, int]] = field(default_factory=list)
    persisted: list[str] = field(default_factory=list)
    opens: int = 0
    reads: list[int] = field(default_factory=list)
    pages: int = 0
    active: bool = True


class FakeReplies:
    """In-memory stand-in for the reply DAO that keeps the row-lock re-read and cursor semantics."""

    def __init__(self, counters: Counters, bindings: list[OpenClawReplyBinding]) -> None:
        self.counters = counters
        self.bindings = {binding.id: binding for binding in bindings}
        self.media: dict[UUID, tuple[TranscriptReply, ...]] = {}

    async def page(self, after: UUID | None) -> tuple[OpenClawReplyBinding, ...]:
        self.counters.pages += 1
        agents = sorted({row.agent_id for row in self.bindings.values() if after is None or row.agent_id > after})[:20]
        return tuple(
            sorted((row for row in self.bindings.values() if row.agent_id in agents), key=lambda r: (r.agent_id, r.id))
        )

    async def pending_media_many(self, ids: list[UUID]) -> dict[UUID, tuple[TranscriptReply, ...]]:
        return {key: self.media[key] for key in ids if key in self.media}

    async def pending_publications_many(self, _ids: list[UUID]) -> dict[UUID, tuple[()]]:
        return {}

    async def pending_media(self, binding_id: UUID) -> tuple[TranscriptReply, ...]:
        return self.media.get(binding_id, ())

    async def pending_publications(self, _binding_id: UUID) -> tuple[()]:
        return ()

    async def lock(self, binding: OpenClawReplyBinding) -> OpenClawReplyBinding | None:
        self.counters.locks += 1
        return self.bindings.get(binding.id)

    async def checkpoint(self, binding_id: UUID, cursor: dict[str, int]) -> None:
        self.counters.checkpoints.append(dict(cursor))
        self.bindings[binding_id] = replace(self.bindings[binding_id], cursor=dict(cursor))

    async def model_refs(self, _binding_id: UUID) -> list[str]:
        return []

    async def is_native_run(self, _agent_id: UUID, _run_id: str) -> bool:
        return False

    async def persist(self, _binding: OpenClawReplyBinding, reply: TranscriptReply, _media: object) -> None:
        self.counters.persisted.append(reply.id)


@dataclass(frozen=True, slots=True)
class Sweep:
    counters: Counters
    replies: FakeReplies
    agent: UUID
    db: Connection
    root: Path

    def add_binding(self, agent: UUID | None = None) -> OpenClawReplyBinding:
        binding = OpenClawReplyBinding(uuid4(), agent or self.agent, uuid4(), OWNER, TENANT, USER, {})
        self.replies.bindings[binding.id] = binding
        self.db.execute("INSERT INTO session_windows VALUES (?, ?)", (f"s-{binding.id}", binding.session_key))
        return binding


@pytest.fixture
def sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Sweep]:
    from app.services import agent_manager as agents

    counters, agent = Counters(), uuid4()
    replies = FakeReplies(counters, [])
    db = transcript(tmp_path / "agents/main/agent/openclaw-agent.sqlite")

    async def lock(*_args: object, **_kwargs: object) -> None:
        counters.locks += 1

    async def access(*_args: object) -> None:
        counters.access += 1

    async def agent_row(_agent_id: UUID) -> SimpleNamespace:
        return SimpleNamespace(id=agent, creator_id=OWNER, tenant_id=TENANT)

    async def session_row(_session: UUID, _agent: UUID) -> SimpleNamespace:
        return SimpleNamespace(user_id=USER, source_channel="web")

    async def user_row(_user: UUID, *, fresh: bool) -> SimpleNamespace:
        assert fresh
        return SimpleNamespace(
            is_active=counters.active, must_change_password=False, tenant_id=TENANT, identity_id=None, identity=None
        )

    async def guard(_agent: object, _turn: None) -> ResponseGuard:
        return ResponseGuard.build([])

    @asynccontextmanager
    async def connection() -> AsyncGenerator[None]:
        yield

    def counted_open(root: Path) -> AbstractContextManager[TranscriptSource]:
        counters.opens += 1
        return open_transcript(root)

    read = TranscriptSource.read

    def counted_read(
        source: TranscriptSource, queries: Sequence[TranscriptQuery]
    ) -> tuple[tuple[TranscriptRow, ...], ...]:
        counters.reads.append(len(queries))
        return read(source, queries)

    monkeypatch.setattr(agents.agent_manager, "_agent_dir", lambda _agent_id: tmp_path)
    monkeypatch.setattr(recovery, "openclaw_reply_dao", replies)
    monkeypatch.setattr(recovery, "connection_ctx", connection)
    monkeypatch.setattr(authorization, "openclaw_reply_dao", replies)
    monkeypatch.setattr(authorization, "connection_ctx", connection)
    for name in ("lock_tenants", "lock_user", "lock_agent"):
        monkeypatch.setattr(authorization, name, lock)
    monkeypatch.setattr(authorization, "check_agent_access", access)
    monkeypatch.setattr(authorization, "load_agent_for_new_work", agent_row)
    monkeypatch.setattr(authorization.chat_session_dao, "lock_for_agent", session_row)
    monkeypatch.setattr(authorization.user_dao, "get_with_identity", user_row)
    monkeypatch.setattr(recovery, "response_guard", guard)
    monkeypatch.setattr(recovery, "open_transcript", counted_open)
    monkeypatch.setattr(TranscriptSource, "read", counted_read)
    try:
        yield Sweep(counters, replies, agent, db, tmp_path)
    finally:
        db.close()


def reset(counters: Counters) -> None:
    counters.locks = counters.access = counters.opens = counters.pages = 0
    counters.checkpoints.clear()
    counters.persisted.clear()
    counters.reads.clear()


async def test_idle_tick_takes_no_locks_authorization_or_checkpoint(sweep: Sweep) -> None:
    counters = sweep.counters
    bindings = [sweep.add_binding() for _ in range(3)]
    append(sweep.db, f"s-{bindings[0].id}", 1, text="hello")
    scanner = recovery.DelayedReplyRecovery()

    assert await scanner.scan_once() == 0
    assert counters.persisted == ["event-s-" + str(bindings[0].id) + "-1"]
    assert counters.reads == [3]
    assert (counters.locks, counters.access) != (0, 0)
    assert len(counters.checkpoints) == 1

    reset(counters)
    await scanner.scan_once()
    assert counters.reads == [1]  # only the binding that just consumed rows is reread, on its empty page
    assert (counters.locks, counters.access, counters.checkpoints) == (0, 0, [])

    reset(counters)
    await scanner.scan_once()
    assert (counters.locks, counters.access, counters.checkpoints, counters.reads) == (0, 0, [], [])
    assert counters.opens == 1
    assert counters.pages == 1


async def test_commit_touching_only_the_wal_is_recovered_after_idle_ticks(sweep: Sweep) -> None:
    binding = sweep.add_binding()
    sweep.db.commit()
    scanner = recovery.DelayedReplyRecovery()
    await scanner.scan_once()
    await scanner.scan_once()
    reset(sweep.counters)
    database = sweep.root / "agents/main/agent/openclaw-agent.sqlite"
    size = database.stat().st_size

    append(sweep.db, f"s-{binding.id}", 1, text="late")
    await scanner.scan_once()

    assert database.stat().st_size == size
    assert sweep.counters.persisted == [f"event-s-{binding.id}-1"]
    assert len(sweep.counters.checkpoints) == 1


async def test_rows_without_replies_advance_the_cursor(sweep: Sweep) -> None:
    binding = sweep.add_binding()
    sweep.db.execute("INSERT INTO transcript_events VALUES (?, 5, '{}', NULL, 'user', 5)", (f"s-{binding.id}",))
    sweep.db.commit()

    await recovery.DelayedReplyRecovery().scan_once()

    assert sweep.counters.persisted == []
    assert sweep.counters.checkpoints == [{f"s-{binding.id}": 5}]


async def test_unauthorized_binding_keeps_its_cursor_until_reactivated(sweep: Sweep) -> None:
    binding = sweep.add_binding()
    append(sweep.db, f"s-{binding.id}", 1, text="held")
    sweep.counters.active = False
    scanner = recovery.DelayedReplyRecovery()

    for _ in range(2):
        await scanner.scan_once()
    assert sweep.counters.checkpoints == []
    assert sweep.counters.persisted == []
    assert sweep.replies.bindings[binding.id].cursor == {}

    sweep.counters.active = True
    await scanner.scan_once()
    assert sweep.counters.persisted == [f"event-s-{binding.id}-1"]
    assert sweep.replies.bindings[binding.id].cursor == {f"s-{binding.id}": 1}


async def test_pending_media_and_the_backstop_force_rereads(sweep: Sweep) -> None:
    binding = sweep.add_binding()
    sweep.db.commit()
    now = [0.0]
    scanner = recovery.DelayedReplyRecovery(backstop=30.0, clock=lambda: now[0])
    await scanner.scan_once()
    reset(sweep.counters)

    await scanner.scan_once()
    assert sweep.counters.reads == []

    now[0] = 31.0
    await scanner.scan_once()
    assert sweep.counters.reads == [1]

    reset(sweep.counters)
    sweep.replies.media[binding.id] = (TranscriptReply("event-pending", "", "Waiting"),)
    await scanner.scan_once()
    await scanner.scan_once()
    assert sweep.counters.reads == [1, 1]
    assert sweep.counters.persisted == ["event-pending", "event-pending"]


async def test_new_binding_is_read_while_the_others_stay_skipped(sweep: Sweep) -> None:
    sweep.add_binding()
    late = sweep.add_binding()
    sweep.db.commit()
    del sweep.replies.bindings[late.id]
    scanner = recovery.DelayedReplyRecovery()
    await scanner.scan_once()
    reset(sweep.counters)

    sweep.replies.bindings[late.id] = late
    await scanner.scan_once()

    assert sweep.counters.reads == [1]


async def test_agent_with_many_bindings_opens_once_and_keeps_separate_cursors(sweep: Sweep) -> None:
    bindings = [sweep.add_binding() for _ in range(25)]
    for binding in bindings:
        append(sweep.db, f"s-{binding.id}", 1, text="hello")

    assert await recovery.DelayedReplyRecovery().scan_once() == 0

    assert (sweep.counters.opens, sweep.counters.reads, sweep.counters.pages) == (1, [25], 1)
    assert sorted(sweep.counters.persisted) == sorted(f"event-s-{binding.id}-1" for binding in bindings)
    assert all(sweep.replies.bindings[b.id].cursor == {f"s-{b.id}": 1} for b in bindings)


async def test_sweep_budget_stops_between_whole_agent_groups_and_resumes(sweep: Sweep) -> None:
    bindings = [sweep.add_binding(uuid4()) for _ in range(21)]
    sweep.db.commit()
    scanner = recovery.DelayedReplyRecovery(budget=0.0)

    for _ in bindings:
        await scanner.scan_once()
    assert sweep.counters.reads == [1] * 21
    assert sweep.counters.opens == 21

    reset(sweep.counters)
    await scanner.scan_once()
    assert sweep.counters.reads == []


async def test_default_budget_finishes_the_whole_lap_in_one_scan(sweep: Sweep) -> None:
    for _ in range(21):
        sweep.add_binding(uuid4())
    sweep.db.commit()

    await recovery.DelayedReplyRecovery().scan_once()

    assert (sweep.counters.pages, sweep.counters.reads) == (2, [1] * 21)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("binding failed"),
        ValidationError.from_exception_data("reply", [{"type": "missing", "loc": ("id",), "input": {}}]),
        HTTPException(503, "unavailable"),
        AgentExpired("Agent"),
        asyncio.CancelledError(),
    ],
)
async def test_binding_failure_does_not_starve_following_bindings_or_agents(
    sweep: Sweep, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    first, sibling = sorted([sweep.add_binding(UUID(int=1)) for _ in range(2)], key=lambda binding: binding.id)
    later, last = sweep.add_binding(UUID(int=2)), sweep.add_binding(UUID(int=3))
    for binding in (first, sibling, later, last):
        append(sweep.db, f"s-{binding.id}", 1, text="reply")
    record_page = recovery._record_page
    now = [0.0]

    async def record(binding: OpenClawReplyBinding, rows: tuple[TranscriptRow, ...], root: Path):
        if binding.id == first.id:
            raise failure
        result = await record_page(binding, rows, root)
        if binding.id == later.id:
            now[0] = 2.0
        return result

    warning = Mock()
    monkeypatch.setattr(recovery, "_record_page", record)
    monkeypatch.setattr(recovery.logger, "warning", warning)
    scanner = recovery.DelayedReplyRecovery(budget=1.0, clock=lambda: now[0])

    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await scanner.scan_once()
        assert sweep.counters.persisted == []
        assert scanner._after is None
        return

    await scanner.scan_once()

    assert sweep.counters.persisted == [f"event-s-{binding.id}-1" for binding in (sibling, later)]
    assert sweep.replies.bindings[first.id].cursor == {}
    assert sweep.replies.bindings[last.id].cursor == {}
    assert scanner._after == later.agent_id
    assert warning.call_count == (0 if isinstance(failure, AgentExpired) else 1)
    await scanner.scan_once()
    assert sweep.replies.bindings[last.id].cursor == {f"s-{last.id}": 1}
    assert scanner._after is None


async def start_budgeted_loop(monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP) -> asyncio.Task[object]:
    monkeypatch.setattr(recovery.anyio, "sleep", worker_http.pacing.wait)
    task = worker_http.start_loop(
        "openclaw_delayed_replies",
        recovery.openclaw_delayed_reply_loop(),
        budget_seconds=recovery.PROGRESS_BUDGET_SECONDS,
    )
    await worker_http.pacing.paused()
    return task


@pytest.mark.usefixtures("sweep")
async def test_idle_scan_passes_beat_so_readiness_outlives_the_budget(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP
) -> None:
    # Given the delayed-reply loop as a budgeted worker loop with no bindings.
    task = await start_budgeted_loop(monkeypatch, worker_http)
    # When four idle passes carry the clock past the budget.
    for _ in range(4):
        await worker_http.pacing.step()
        # Then each finished pass has beaten.
        await worker_http.ready(expected=True)
    assert not task.done()


@pytest.mark.parametrize("failing", ["page", "binding"])
async def test_database_failures_do_not_beat_so_readiness_goes_stale_then_recovers(
    sweep: Sweep, monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, failing: str
) -> None:
    # Given a binding with an unread reply, and a database failing either the page read (raised to the loop)
    # or the binding's receipt write (caught while scanning the binding).
    binding = sweep.add_binding()
    append(sweep.db, f"s-{binding.id}", 1, text="reply")
    sweep.db.commit()
    broken = AsyncMock(return_value=(), side_effect=DbError("database unavailable"))
    if failing == "page":
        monkeypatch.setattr(sweep.replies, "page", broken)
    else:
        monkeypatch.setattr(recovery, "_record_page", broken)
    task = await start_budgeted_loop(monkeypatch, worker_http)
    # When failing passes alone carry the clock past the budget.
    for _ in range(4):
        await worker_http.pacing.step()
    # Then readiness fails while health holds and the loop keeps running.
    await worker_http.ready(expected=False)
    assert not task.done()
    # And the next pass that reads and writes successfully beats.
    broken.side_effect = None
    await worker_http.pacing.step()
    await worker_http.ready(expected=True)


@pytest.mark.parametrize("failing", ["transcript", "publish"])
async def test_guest_and_delivery_failures_still_beat(
    sweep: Sweep, monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, failing: str
) -> None:
    # Given a binding whose guest transcript cannot be opened, or whose live publication cannot be delivered.
    binding = sweep.add_binding()
    sweep.db.commit()
    if failing == "transcript":
        broken = Mock(side_effect=OSError("transcript unreadable"))
        monkeypatch.setattr(recovery, "open_transcript", broken)
    else:
        reply = ChatMessageRecord(uuid4(), sweep.agent, USER, "assistant", "reply")
        pending = AsyncMock(return_value={binding.id: (reply,)})
        monkeypatch.setattr(sweep.replies, "pending_publications_many", pending)
        broken = AsyncMock(side_effect=RuntimeError("client socket closed"))
        monkeypatch.setattr(recovery, "_publish", broken)
    task = await start_budgeted_loop(monkeypatch, worker_http)
    # When the failing passes carry the clock past the budget.
    for _ in range(4):
        await worker_http.pacing.step()
        # Then every finished pass still beats.
        await worker_http.ready(expected=True)
    assert broken.call_count == 5
    assert not task.done()
