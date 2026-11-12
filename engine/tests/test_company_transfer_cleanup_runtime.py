import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import pytest

from app.api import background_tasks
from app.db.errors import DbError
from app.records.company_transfer import TransferChoice
from app.records.company_transfer_cleanup import TransferCleanupRecord
from app.runtime import lifecycle
from app.services import audit_logger, company_transfer_cleanup as cleanup
from producer_admission_fixtures import producer_admission
from worker_progress_fixtures import WorkerHTTP, worker_http

__all__ = ["producer_admission", "worker_http"]


@pytest.mark.parametrize("role", ["api", "connector"])
async def test_connector_role_owns_cleanup_loop_and_drains_it_before_database(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    # Given a runtime with isolated resources and a cleanup operation held in flight.
    runtime = lifecycle.Runtime()
    entered = anyio.Event()
    finished = anyio.Event()
    initializing = anyio.Event()
    initialized = anyio.Event()
    database_closed = False

    class Connector:
        async def start_all(self) -> None:
            initializing.set()
            await initialized.wait()

        async def stop_all(self) -> None:
            return None

    async def no_resource(*_args: object) -> None:
        return None

    async def reconcile() -> None:
        assert initialized.is_set()
        entered.set()
        try:
            await anyio.sleep_forever()
        finally:
            finished.set()

    async def close_database() -> None:
        nonlocal database_closed
        assert finished.is_set() == (role == "connector")
        database_closed = True

    monkeypatch.setattr(lifecycle, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(lifecycle, "intercept_standard_logging", lambda: None)
    monkeypatch.setattr(lifecycle, "log_bwrap_startup_status", lambda: None)
    monkeypatch.setattr(lifecycle, "shutdown_logging", lambda: None)
    monkeypatch.setattr(lifecycle, "init_pool", no_resource)
    monkeypatch.setattr(lifecycle, "get_redis", no_resource)
    monkeypatch.setattr(lifecycle, "close_redis", no_resource)
    monkeypatch.setattr(lifecycle, "close_pool", close_database)
    monkeypatch.setattr(lifecycle, "shutdown_llm_clients", no_resource)
    monkeypatch.setattr(lifecycle, "connector_managers", lambda: (Connector(),))
    monkeypatch.setattr(lifecycle.realtime_router, "start", no_resource)
    monkeypatch.setattr(lifecycle.realtime_router, "stop", no_resource)
    monkeypatch.setattr(runtime.proxy, "start", no_resource)
    monkeypatch.setattr(runtime.proxy, "stop", no_resource)
    monkeypatch.setattr(runtime.incidents, "start", lambda: None)
    monkeypatch.setattr(runtime.incidents, "stop", no_resource)
    monkeypatch.setattr(audit_logger, "write_audit_log", no_resource)
    monkeypatch.setattr(cleanup, "reconcile_transfer_cleanup", reconcile)
    background_tasks.start_background_tasks()
    try:
        # When startup selects its required loops and shutdown drains their ownership.
        await runtime.start(frozenset({role}))
        if role == "connector":
            with anyio.fail_after(1):
                await initializing.wait()
                assert not entered.is_set()
                initialized.set()
                await entered.wait()
        assert ("transfer_cleanup" in runtime.tasks.required) == (role == "connector")
    finally:
        with anyio.fail_after(1):
            await runtime.stop()
        background_tasks.start_background_tasks()
    # Then no cleanup operation survives the database resource it uses.
    assert database_closed
    assert not runtime.tasks.tasks


def run_cleanup_loop(monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP) -> asyncio.Task[object]:
    monkeypatch.setattr(cleanup, "sleep", worker_http.pacing.wait)
    return worker_http.start_loop(
        "transfer_cleanup",
        cleanup.start_transfer_cleanup_reconciler(),
        budget_seconds=cleanup.PROGRESS_BUDGET_SECONDS,
    )


async def test_cleanup_loop_beats_after_every_completed_pass_including_an_empty_one(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP
) -> None:
    # Given a queue read that always succeeds and finds nothing pending.
    monkeypatch.setattr(cleanup.cleanup, "pending", AsyncMock(return_value=[]))
    task = run_cleanup_loop(monkeypatch, worker_http)
    await worker_http.pacing.paused()
    # When passes keep finishing for longer than the budget in total.
    for _ in range(4):
        await worker_http.pacing.step()
        # Then every finished pass keeps the loop ready.
        await worker_http.ready(expected=True)
    assert not task.done()


@pytest.mark.parametrize("failure", ["docker_unavailable", OSError("docker unreachable")])
async def test_cleanup_loop_beats_when_an_item_step_fails_after_the_queue_was_read(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, failure: str | OSError
) -> None:
    # Given a queued item whose Docker or storage step fails on every pass.
    item = TransferCleanupRecord(uuid4(), uuid4(), TransferChoice.FREE)
    complete = AsyncMock(return_value=failure) if isinstance(failure, str) else AsyncMock(side_effect=failure)
    monkeypatch.setattr(cleanup.cleanup, "pending", AsyncMock(return_value=[item]))
    monkeypatch.setattr(cleanup.cleanup, "record_attempt", AsyncMock())
    monkeypatch.setattr(cleanup, "complete_cleanup", complete)
    task = run_cleanup_loop(monkeypatch, worker_http)
    await worker_http.pacing.paused()
    # When failing passes outlast the budget.
    for _ in range(4):
        await worker_http.pacing.step()
        # Then the loop is never stale: only a failed queue read is.
        await worker_http.ready(expected=True)
    assert complete.await_count == 5
    assert not task.done()


async def test_cleanup_loop_goes_stale_while_the_queue_read_fails_and_recovers_on_the_next_read(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP
) -> None:
    # Given a queue read that starts failing; the helper catches that failure.
    pending = AsyncMock(return_value=[])
    monkeypatch.setattr(cleanup.cleanup, "pending", pending)
    task = run_cleanup_loop(monkeypatch, worker_http)
    await worker_http.pacing.paused()
    pending.side_effect = DbError("queue unavailable")
    # When failing passes outlast the budget.
    for _ in range(4):
        await worker_http.pacing.step()
    # Then readiness fails while health holds, and the loop keeps running.
    await worker_http.ready(expected=False)
    assert not task.done()
    # And the first completed read restores readiness.
    pending.side_effect = None
    await worker_http.pacing.step()
    await worker_http.ready(expected=True)


async def test_helper_called_outside_the_cleanup_loop_never_beats_the_calling_loop(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP
) -> None:
    # Given another budgeted loop (the heartbeat) that reconciles transfers once per pass and stops beating.
    monkeypatch.setattr(cleanup.cleanup, "pending", AsyncMock(return_value=[]))

    async def reconciling_loop() -> None:
        await worker_http.pacing.wait(0)
        await cleanup.reconcile_transfer_cleanup()
        await worker_http.pacing.wait(0)

    task = worker_http.start_loop("heartbeat", reconciling_loop(), budget_seconds=300.0)
    await worker_http.pacing.paused()
    # When the budget passes and the loop runs the shared helper.
    worker_http.pacing.now += 301
    await worker_http.ready(expected=False)
    await worker_http.pacing.step()
    # Then the helper's completed pass was not credited to the caller.
    await worker_http.ready(expected=False)
    assert not task.done()
