import asyncio
import inspect
from collections.abc import AsyncIterator
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.api import background_tasks
from app.api.background_tasks import schedule_background_task
from app.core import access_cache, redis_cache
from app.core.logging import get_trace_id, set_trace_id
from app.core.logging.context import trace_id_var
from app.db import session as session_module
from app.services.agent_tool_exec.registry import (
    ToolExecutionContext,
    current_execution_context,
    use_execution_context,
)
from test_db_session import _FakePool, _FakeRawConnection


@pytest.fixture(autouse=True)
async def _clear_conn_ctx() -> AsyncIterator[None]:
    token = session_module._conn_ctx.set(None)
    trace_token = trace_id_var.set(None)
    background_tasks.start_background_tasks()
    try:
        yield
    finally:
        await background_tasks.drain_background_tasks(grace_seconds=0)
        background_tasks.start_background_tasks()
        session_module._conn_ctx.reset(token)
        trace_id_var.reset(trace_token)


@pytest.fixture
def raw(monkeypatch: pytest.MonkeyPatch) -> _FakeRawConnection:
    connection = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(connection))
    return connection


@pytest.mark.asyncio
async def test_schedule_background_task_runs_work_after_caller_returns() -> None:
    release = asyncio.Event()
    completed = asyncio.Event()

    async def work() -> None:
        await release.wait()
        completed.set()

    result = schedule_background_task(work(), "test task")

    release.set()
    await asyncio.wait_for(completed.wait(), timeout=1)
    assert result is None
    assert completed.is_set()


@pytest.mark.asyncio
async def test_background_task_waits_for_owning_commit(raw: _FakeRawConnection) -> None:
    # Given
    started = asyncio.Event()
    commits: list[int] = []

    async def work() -> None:
        commits.append(raw.commits)
        started.set()

    # When
    async with session_module.connection_ctx():
        schedule_background_task(work(), "committed work")
        await asyncio.sleep(0)
        started_before_commit = started.is_set()
    await asyncio.wait_for(started.wait(), timeout=1)
    # Then
    assert not started_before_commit
    assert commits == [1]


@pytest.mark.asyncio
async def test_background_task_has_only_captured_trace_context(raw: _FakeRawConnection, tmp_path: Path) -> None:
    # Given
    del raw
    completed = asyncio.Event()
    marker: ContextVar[str | None] = ContextVar("request marker", default=None)
    seen: list[object] = []

    async def work() -> None:
        seen.extend(
            [
                session_module._conn_ctx.get(),
                current_execution_context(),
                access_cache._deferred_acl.get(),
                redis_cache._deferred_versions.get(),
                access_cache._request_memo.get(),
                marker.get(),
                get_trace_id(),
            ]
        )
        completed.set()

    token = marker.set("request only")
    memo_token = access_cache._request_memo.set({})
    try:
        with use_execution_context(ToolExecutionContext("tenant", tmp_path)):
            async with session_module.connection_ctx():
                set_trace_id("scheduled trace")
                # When
                schedule_background_task(work(), "isolated work")
                set_trace_id("later trace")
        await asyncio.wait_for(completed.wait(), timeout=1)
    finally:
        marker.reset(token)
        access_cache._request_memo.reset(memo_token)
    # Then
    assert seen == [None, None, None, None, None, None, "scheduled trace"]


@pytest.mark.asyncio
async def test_background_task_rollback_closes_coroutine_and_warns(raw, monkeypatch) -> None:
    # Given
    del raw
    started = asyncio.Event()
    logger = Mock()
    monkeypatch.setattr(background_tasks, "logger", logger)

    async def work() -> None:
        started.set()

    coroutine = work()

    async def scope() -> None:
        async with session_module.connection_ctx():
            schedule_background_task(coroutine, "dropped work")
            raise RuntimeError("rollback")

    # When
    with pytest.raises(RuntimeError, match="rollback"):
        await scope()
    await asyncio.sleep(0)
    # Then
    assert not started.is_set()
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    logger.warning.assert_called_once()
    assert "dropped work" in logger.warning.call_args.args[0]


@pytest.mark.asyncio
async def test_background_task_failure_observer_logs_in_fresh_context(monkeypatch) -> None:
    # Given
    observed = asyncio.Event()
    failure = RuntimeError("child failed")
    logger = Mock()
    marker: ContextVar[str | None] = ContextVar("observer marker", default=None)
    seen: list[object] = []

    def log_failure(_message: str) -> None:
        seen.extend([marker.get(), get_trace_id()])
        observed.set()

    logger.opt.return_value.error.side_effect = log_failure
    monkeypatch.setattr(background_tasks, "logger", logger)

    async def work() -> None:
        raise failure

    token = marker.set("caller only")
    try:
        set_trace_id("observer trace")
        # When
        schedule_background_task(work(), "failing work")
        await asyncio.wait_for(observed.wait(), timeout=1)
    finally:
        marker.reset(token)
    # Then
    logger.opt.assert_called_once_with(exception=failure)
    assert seen == [None, "observer trace"]
    assert not background_tasks._tasks


@pytest.mark.asyncio
async def test_drain_allows_completion_without_touching_unowned_tasks() -> None:
    # Given
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def work() -> None:
        started.set()
        await release.wait()
        finished.set()

    schedule_background_task(work(), "graceful work")
    await started.wait()
    unrelated = asyncio.create_task(release.wait())
    # When
    drain = asyncio.create_task(background_tasks.drain_background_tasks())
    await asyncio.sleep(0)
    try:
        assert not drain.done()
        assert not unrelated.done()
        release.set()
        await asyncio.wait_for(drain, timeout=1)
        await unrelated
    finally:
        unrelated.cancel()
        await asyncio.gather(unrelated, return_exceptions=True)
    # Then
    assert finished.is_set()
    assert not background_tasks._tasks


@pytest.mark.asyncio
async def test_drain_timeout_awaits_cancellation_cleanup() -> None:
    # Given
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    finished: list[str] = []

    async def work() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            finished.append("cleaned")

    schedule_background_task(work(), "slow work")
    await started.wait()
    # When
    drain = asyncio.create_task(background_tasks.drain_background_tasks(grace_seconds=0))
    await asyncio.wait_for(cleaning.wait(), timeout=1)
    assert not drain.done()
    release.set()
    await asyncio.wait_for(drain, timeout=1)
    # Then
    assert finished == ["cleaned"]
    assert not background_tasks._tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(("deferred", "reopen"), [(False, False), (True, False), (True, True)])
async def test_shutdown_rejects_and_closes_work(raw, monkeypatch, deferred: bool, reopen: bool) -> None:
    # Given
    del raw
    logger = Mock()
    monkeypatch.setattr(background_tasks, "logger", logger)
    started: list[str] = []

    async def work() -> None:
        started.append("started")

    coroutine = work()
    # When
    async with session_module.connection_ctx():
        if deferred:
            schedule_background_task(coroutine, "rejected work")
        await background_tasks.drain_background_tasks()
        if reopen:
            background_tasks.start_background_tasks()
        if not deferred:
            schedule_background_task(coroutine, "rejected work")
    # Then
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    assert started == []
    logger.warning.assert_called_once()
    if reopen:
        schedule_background_task(work(), "new generation")
        await background_tasks.drain_background_tasks()
        assert started == ["started"]
