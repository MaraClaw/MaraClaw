import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.core import access_cache, redis_cache
from app.core.logging import get_trace_id, set_trace_id
from app.core.logging.context import trace_id_var
from app.db import session
from app.services import scheduler_tasks
from app.services.agent_tool_exec.registry import (
    ToolExecutionContext,
    current_execution_context,
    use_execution_context,
)
from test_db_session import _FakePool, _FakeRawConnection


@pytest.fixture(autouse=True)
async def owner() -> AsyncIterator[None]:
    conn_token = session._conn_ctx.set(None)
    trace_token = trace_id_var.set(None)
    scheduler_tasks.start_schedule_tasks()
    try:
        yield
    finally:
        await scheduler_tasks.stop_schedule_tasks()
        session._conn_ctx.reset(conn_token)
        trace_id_var.reset(trace_token)


@pytest.fixture
def raw(monkeypatch: pytest.MonkeyPatch) -> _FakeRawConnection:
    connection = _FakeRawConnection()
    monkeypatch.setattr(session, "get_pool", lambda: _FakePool(connection))
    return connection


@pytest.mark.asyncio
async def test_factory_is_constructed_only_after_commit(raw: _FakeRawConnection) -> None:
    completed = asyncio.Event()
    commits: list[int] = []

    async def work() -> None:
        completed.set()

    def factory() -> Coroutine[object, object, None]:
        commits.append(raw.commits)
        return work()

    async with session.connection_ctx():
        scheduler_tasks.schedule_execution(factory)
        assert commits == []
    await asyncio.wait_for(completed.wait(), timeout=1)
    assert commits == [1]


@pytest.mark.asyncio
async def test_rollback_does_not_construct_coroutine(raw: _FakeRawConnection) -> None:
    factory = Mock(side_effect=AssertionError("factory called on rollback"))

    async def rollback() -> None:
        async with session.connection_ctx():
            scheduler_tasks.schedule_execution(factory)
            raise ValueError("rollback")

    with pytest.raises(ValueError, match="rollback"):
        await rollback()
    factory.assert_not_called()
    assert raw.rollbacks == 1


@pytest.mark.asyncio
async def test_execution_has_empty_context(raw: _FakeRawConnection, tmp_path: Path) -> None:
    del raw
    completed = asyncio.Event()
    marker: ContextVar[str | None] = ContextVar("schedule caller", default=None)
    seen: list[object] = []

    async def work() -> None:
        seen.extend(
            [
                session._conn_ctx.get(),
                session._completion_ctx.get(),
                current_execution_context(),
                access_cache._deferred_acl.get(),
                redis_cache._deferred_versions.get(),
                access_cache._request_memo.get(),
                marker.get(),
                get_trace_id(),
            ]
        )
        completed.set()

    marker_token = marker.set("caller")
    memo_token = access_cache._request_memo.set({})
    try:
        with use_execution_context(ToolExecutionContext("tenant", tmp_path)):
            async with session.connection_ctx():
                set_trace_id("caller-trace")
                scheduler_tasks.schedule_execution(work)
        await asyncio.wait_for(completed.wait(), timeout=1)
    finally:
        marker.reset(marker_token)
        access_cache._request_memo.reset(memo_token)
    assert seen == [None] * 8


@pytest.mark.asyncio
async def test_failure_is_observed_outside_caller_and_child_context(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = asyncio.Event()
    marker: ContextVar[str | None] = ContextVar("observer", default=None)
    seen: list[object] = []
    failure = RuntimeError("execution failed")
    logger = Mock()

    def log_failure(_message: str) -> None:
        seen.extend([marker.get(), get_trace_id()])
        observed.set()

    logger.opt.return_value.error.side_effect = log_failure
    monkeypatch.setattr(scheduler_tasks, "logger", logger)

    async def work() -> None:
        marker.set("child")
        set_trace_id("child-trace")
        raise failure

    token = marker.set("caller")
    try:
        set_trace_id("caller-trace")
        scheduler_tasks.schedule_execution(work)
        await asyncio.wait_for(observed.wait(), timeout=1)
    finally:
        marker.reset(token)
    logger.opt.assert_called_once_with(exception=failure)
    assert seen[0] is None
    assert seen[1]
    assert seen[1] not in {"caller-trace", "child-trace"}
    assert not scheduler_tasks._tasks


@pytest.mark.asyncio
async def test_stop_closes_admission_before_awaiting_cancellation() -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()
    rejected = Mock(side_effect=AssertionError("admitted during shutdown"))

    async def work() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            scheduler_tasks.schedule_execution(rejected)
            stopped.set()

    scheduler_tasks.schedule_execution(work)
    await asyncio.wait_for(started.wait(), timeout=1)
    await scheduler_tasks.stop_schedule_tasks()
    scheduler_tasks.schedule_execution(rejected)
    assert stopped.is_set()
    rejected.assert_not_called()
    assert not scheduler_tasks._tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_late_commit_cannot_launch_in_closed_or_new_generation(raw: _FakeRawConnection, restart: bool) -> None:
    factory = Mock(side_effect=AssertionError("stale callback launched"))
    async with session.connection_ctx():
        scheduler_tasks.schedule_execution(factory)
        await scheduler_tasks.stop_schedule_tasks()
        if restart:
            scheduler_tasks.start_schedule_tasks()
    factory.assert_not_called()
    assert raw.commits == 1
    if restart:
        completed = asyncio.Event()

        async def work() -> None:
            completed.set()

        scheduler_tasks.schedule_execution(work)
        await asyncio.wait_for(completed.wait(), timeout=1)
