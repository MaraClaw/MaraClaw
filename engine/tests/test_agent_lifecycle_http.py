from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import anyio
import httpx
import pytest
from fastapi import Depends, FastAPI

import test_openclaw_update_postgres as postgres_fixtures
from app.api import agents as api
from app.core import events
from app.core.domain.agents import AgentStatus
from app.db import session
from app.db.connection import DbConnection
from app.records.agent import AgentRecord
from app.records.agent_start import AgentStartOperation
from app.records.user import UserRecord
from app.services import agent_lifecycle as lifecycle, openclaw_update_exclusion as exclusion
from app.services.agent_setup_lock import setup_workspace_lock
from app.services.guest_admission import GuestAdmissionError, GuestStartPreflightError, assert_start_active
from app.services.guest_identity import GuestOwnershipError
from app.services.guest_start import start_operation_id

db = postgres_fixtures.db


class TransactionalRuntime:
    def __init__(self, agent):
        self.committed = replace(agent)
        self.pending = None
        self.events = []
        self.published = []
        self.locked = False
        self.released = anyio.Event()

    async def update(self, *, db_obj, obj_in):
        assert session.get_connection() is not None
        self.pending = replace(db_obj, **obj_in)
        self.events.append("persist")
        return replace(self.pending)

    async def commit(self):
        self.events.append("commit_locked" if self.locked else "commit_unlocked")
        if self.pending is not None:
            self.committed = replace(self.pending)
            self.pending = None

    async def rollback(self):
        self.events.append("rollback")
        self.pending = None

    async def publish(self, channel, message):
        self.events.append("publish")
        self.published.append((channel, message))
        return 1


@pytest.fixture
def surface(monkeypatch, tmp_path):
    user = UserRecord(id=uuid4())
    agent = AgentRecord(
        id=uuid4(),
        name="guest",
        creator_id=user.id,
        status=AgentStatus.RUNNING,
        container_id="old",
        container_port=18888,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    runtime = TransactionalRuntime(agent)

    async def begin(_id):
        return AgentStartOperation(agent.id, uuid4(), "pending"), replace(runtime.committed)

    async def cancel(_id):
        return None

    async def reserve(_operation, *, due_only=False):
        return True

    async def fail(_operation, _code):
        return None

    async def publish(_operation, record):
        await runtime.update(db_obj=record, obj_in={})
        await runtime.commit()
        return True

    async def halted(_operation_id):
        return None

    async def recover(_operation):
        return None

    monkeypatch.setattr(lifecycle.start_store, "begin", begin)
    monkeypatch.setattr(lifecycle.start_store, "cancel", cancel)
    monkeypatch.setattr(lifecycle.start_store, "publish", publish)
    monkeypatch.setattr(lifecycle.start_store, "reserve", reserve)
    monkeypatch.setattr(lifecycle.start_store, "fail", fail)
    monkeypatch.setattr(lifecycle.start_store, "halted", halted)
    monkeypatch.setattr(lifecycle.start_store, "recover", recover)

    async def authorize(_user, _id):
        return replace(runtime.committed), "manage"

    async def get_agent(_id):
        return replace(runtime.committed)

    async def gate(_id):
        return None

    @asynccontextmanager
    async def lock(
        agent_dir: Path,
        *,
        shared: bool = False,
        timeout_seconds: float | None = None,
        until_transaction_end: bool = False,
    ) -> AsyncGenerator[None]:
        async with setup_workspace_lock(
            agent_dir, shared=shared, timeout_seconds=timeout_seconds, until_transaction_end=until_transaction_end
        ):
            runtime.locked = True
            try:
                yield
            finally:
                runtime.events.append("release")
                runtime.locked = False
                runtime.released.set()

    async def transaction():
        raw = DbConnection.__new__(DbConnection)
        monkeypatch.setattr(raw, "commit", runtime.commit)
        monkeypatch.setattr(raw, "rollback", runtime.rollback)
        async with session.transaction(raw):
            yield

    async def serialize(record, _viewer):
        return api.AgentOut.model_validate(record)

    async def redis():
        return runtime

    monkeypatch.setattr(events, "get_redis", redis)
    monkeypatch.setattr(lifecycle.agent_manager, "docker_client", None)
    monkeypatch.setattr(api, "check_agent_access", authorize)
    monkeypatch.setattr(api.agent_dao, "get", get_agent)
    monkeypatch.setattr(api.agent_dao, "update", runtime.update)
    monkeypatch.setattr(api, "_agent_to_out", serialize)
    monkeypatch.setattr(exclusion, "assert_no_active_run", gate)
    monkeypatch.setattr(exclusion, "assert_transfer_ready", gate)
    monkeypatch.setattr(lifecycle, "assert_no_active_run", gate)
    monkeypatch.setattr(lifecycle, "assert_transfer_ready", gate)
    monkeypatch.setattr(exclusion, "setup_workspace_lock", lock)
    monkeypatch.setattr(lifecycle.agent_manager, "_agent_dir", lambda _id: tmp_path / str(_id))
    app = FastAPI()
    app.include_router(api.router, prefix="/api", dependencies=[Depends(transaction)])
    app.dependency_overrides[api.get_current_user] = lambda: user
    return app, runtime, agent


@pytest.mark.parametrize(
    ("operation", "succeeds"), [("start", True), ("start", False), ("stop", True), ("stop", False)]
)
async def test_explicit_outcome_is_committed_before_unlock(surface, monkeypatch, operation, succeeds):
    # Given a transactional HTTP request and a Docker operation with a known outcome.
    app, runtime, agent = surface

    async def perform(record):
        assert runtime.locked
        if operation == "start":
            record.status = "running" if succeeds else "error"
            record.container_id = "new" if succeeds else None
            record.container_port = 18889 if succeeds else None
            return record.container_id
        if succeeds:
            record.status = "stopped"
        return succeeds

    monkeypatch.setattr(lifecycle.agent_manager, f"{operation}_container", perform)
    # When the real ASGI route completes (including its HTTP failure response).
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/agents/{agent.id}/{operation}")
    # Then the runtime survives rollback of a 502, and publication preceded lock release.
    assert response.status_code == (200 if succeeds else 502)
    if operation == "start" and not succeeds:
        assert runtime.events[0] == "release"
        assert runtime.committed == agent
        return
    release_at = runtime.events.index("release")
    assert runtime.events[release_at - 2 : release_at] == ["persist", "commit_locked"]
    expected_status = ("running" if succeeds else "error") if operation == "start" else "stopped"
    assert runtime.committed.status == expected_status
    if operation == "stop":
        assert runtime.committed.container_id == "old"
    elif not succeeds:
        assert (runtime.committed.container_id, runtime.committed.container_port) == (None, None)
    if not succeeds:
        assert response.json()["detail"]["code"] == f"guest_{operation}_failed"
        assert runtime.events[-1] == "rollback"


@pytest.mark.parametrize("failure", ["admission", "preflight", "layout", "ownership", "unexpected"])
async def test_preflight_outcomes_do_not_persist_runtime(surface, monkeypatch, failure):
    # Given a failure before destructive replacement.
    app, runtime, agent = surface
    errors = {
        "admission": GuestAdmissionError("guest_memory_capacity_exceeded", "capacity exhausted"),
        "preflight": GuestStartPreflightError("preparation failed"),
        "layout": GuestStartPreflightError(
            "Recreate through the OpenClaw update workflow", code="guest_recreation_required"
        ),
        "ownership": GuestOwnershipError(agent.id, "old", str(uuid4())),
        "unexpected": RuntimeError("programming failure"),
    }

    async def fail(_record):
        raise errors[failure]

    monkeypatch.setattr(lifecycle.agent_manager, "start_container", fail)
    # When the request crosses the HTTP boundary.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/agents/{agent.id}/start")
    # Then each failure leaves runtime references intact and returns its HTTP outcome.
    assert response.status_code == (409 if failure in ("admission", "layout") else 502)
    if failure == "layout":
        assert response.json()["detail"]["code"] == "guest_recreation_required"
    elif failure == "unexpected":
        assert response.json()["detail"]["code"] == "guest_start_failed"
    assert runtime.committed == agent
    assert "persist" not in runtime.events


async def test_stop_timeout_keeps_committed_start_cancellation_and_allows_retry(surface, monkeypatch, db):
    from app.dao.agent_start_dao import agent_start_dao
    from app.main import app as main_app
    from app.services.agent_lifecycle_events import AGENT_LIFECYCLE_CHANNEL
    from app.services.agent_start_store import PostgresStartStore

    app, runtime, agent = surface
    operation = await agent_start_dao.begin(db, agent)
    app.exception_handlers.update(main_app.exception_handlers)
    monkeypatch.setattr(lifecycle.start_store, "cancel", PostgresStartStore().cancel)
    monkeypatch.setattr(exclusion, "LIFECYCLE_LOCK_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(lifecycle.agent_manager, "docker_client", object())

    async def stop(record):
        record.status = AgentStatus.STOPPED
        return True

    stop_guest = AsyncMock(side_effect=stop)
    monkeypatch.setattr(lifecycle.agent_manager, "stop_container", stop_guest)
    held, release, responded = anyio.Event(), anyio.Event(), anyio.Event()
    path = f"/api/agents/{agent.id}/stop"

    async def hold_lock():
        async with setup_workspace_lock(lifecycle.agent_manager._agent_dir(agent.id)):
            held.set()
            await release.wait()

    async def post_busy_stop(client):
        response = await client.post(path)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "agent_workspace_busy"
        responded.set()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async with anyio.create_task_group() as group:
            group.start_soon(hold_lock)
            try:
                with anyio.fail_after(5):
                    await held.wait()
                    group.start_soon(post_busy_stop, client)
                    await responded.wait()
                assert await agent_start_dao.get(db, agent.id) == replace(operation, state="cancelled")
                assert runtime.committed == agent
                assert "persist" not in runtime.events
                assert runtime.published == []
                stop_guest.assert_not_awaited()
            finally:
                release.set()

        retried = await client.post(path)

    assert retried.status_code == 200
    assert (runtime.committed.status, runtime.committed.container_id) == (AgentStatus.STOPPED, agent.container_id)
    assert [channel for channel, _ in runtime.published] == [AGENT_LIFECYCLE_CHANNEL]
    stop_guest.assert_awaited_once()


async def test_stop_cancels_a_slow_start_before_waiting_for_its_lock(surface, monkeypatch):
    # Given a start holding the lifecycle lock through a slow phase that reaches a checkpoint between steps.
    app, runtime, agent = surface
    cancelled, entered, step = anyio.Event(), anyio.Event(), anyio.Event()
    checkpoints = []

    async def cancel(_id):
        runtime.events.append("cancel")
        cancelled.set()

    async def halted(_operation_id):
        return "cancelled" if cancelled.is_set() else None

    async def slow_start(_record):
        while True:
            await assert_start_active(start_operation_id.get())
            checkpoints.append(runtime.locked)
            entered.set()
            await step.wait()

    async def stop(record):
        record.status = "stopped"
        return True

    monkeypatch.setattr(lifecycle.start_store, "cancel", cancel)
    monkeypatch.setattr(lifecycle.start_store, "halted", halted)
    monkeypatch.setattr(lifecycle.agent_manager, "start_container", slow_start)
    monkeypatch.setattr(lifecycle.agent_manager, "stop_container", stop)
    responses = {}

    async def post(client, operation):
        responses[operation] = await client.post(f"/api/agents/{agent.id}/{operation}")

    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        anyio.create_task_group() as group,
    ):
        group.start_soon(post, client, "start")
        with anyio.fail_after(5):
            await entered.wait()
        # When Stop is pressed while the start is in its slow phase, and that step then finishes.
        group.start_soon(post, client, "stop")
        with anyio.fail_after(5):
            await cancelled.wait()
        step.set()
    # Then the cancel committed while the start still held the lock, the start ended at its next checkpoint
    # with the cancelled code, and the stop then ran under the lock and cancelled again.
    assert checkpoints == [True]
    assert responses["start"].status_code == 409
    assert responses["start"].json()["detail"]["code"] == "guest_start_cancelled"
    assert responses["stop"].status_code == 200
    first_release = runtime.events.index("release")
    assert runtime.events[:first_release].count("cancel") == 1
    assert runtime.events[first_release:].count("cancel") == 1
    assert runtime.committed.status == "stopped"


@pytest.mark.parametrize(
    ("seen_at", "reason", "code"),
    [
        ("checkpoint", "cancelled", "guest_start_cancelled"),
        ("checkpoint", "ineligible", "guest_start_ineligible"),
        ("publication", "cancelled", "guest_start_cancelled"),
        ("publication", "ineligible", "guest_start_ineligible"),
        ("reservation", "cancelled", "guest_start_cancelled"),
    ],
)
async def test_stopped_start_answers_409_without_failure_or_runtime(surface, monkeypatch, seen_at, reason, code):
    # Given a start whose operation is cancelled, or whose agent stops qualifying (say it expires), mid-start.
    app, runtime, agent = surface
    recover, fail = AsyncMock(return_value=None), AsyncMock()
    monkeypatch.setattr(lifecycle.start_store, "halted", AsyncMock(return_value=reason))
    monkeypatch.setattr(lifecycle.start_store, "recover", recover)
    monkeypatch.setattr(lifecycle.start_store, "fail", fail)

    async def start(record):
        if seen_at == "checkpoint":
            await assert_start_active(start_operation_id.get())
        record.status, record.container_id, record.container_port = "running", "new", 18889
        return "new"

    monkeypatch.setattr(lifecycle.agent_manager, "start_container", start)
    if seen_at == "publication":
        monkeypatch.setattr(lifecycle.start_store, "publish", AsyncMock(return_value=False))
    if seen_at == "reservation":
        monkeypatch.setattr(lifecycle.start_store, "reserve", AsyncMock(return_value=False))
    # When the start route sees it at a checkpoint, at publication or at reservation.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/agents/{agent.id}/start")
    # Then the start answers 409 with the stop's own code, never 502, and records no failure or runtime.
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == code
    fail.assert_not_awaited()
    assert recover.await_count == (0 if seen_at == "reservation" else 1)
    assert "persist" not in runtime.events
    assert runtime.committed == agent


@pytest.mark.parametrize("operation", ["start", "stop"])
async def test_cancelled_request_drains_commit_before_releasing_lock(surface, monkeypatch, operation):
    # Given Docker work already entered the locked section.
    app, runtime, agent = surface
    entered, finish = anyio.Event(), anyio.Event()

    async def perform(record):
        entered.set()
        await finish.wait()
        if operation == "start":
            record.status, record.container_id, record.container_port = "running", "new", 18889
            return "new"
        record.status = "stopped"
        return True

    monkeypatch.setattr(lifecycle.agent_manager, f"{operation}_container", perform)

    async def request():
        with request_scope:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                await client.post(f"/api/agents/{agent.id}/{operation}")

    request_scope = anyio.CancelScope()
    # When the caller is cancelled while Docker is blocked.
    async with anyio.create_task_group() as group:
        group.start_soon(request)
        await entered.wait()
        request_scope.cancel()
        await anyio.lowlevel.checkpoint()
        finish.set()
        with anyio.fail_after(5):
            await runtime.released.wait()
    # Then Docker finishes and its runtime is durable before the lock is released.
    release_at = runtime.events.index("release")
    assert runtime.events[release_at - 2 : release_at] == ["persist", "commit_locked"]
    assert runtime.committed.container_id == ("new" if operation == "start" else "old")


@pytest.mark.parametrize(
    ("fault", "status", "detail"),
    [
        ("parse", 502, "guest_start_failed"),
        ("io", 502, "guest_start_failed"),
        ("docker", 502, "guest_start_failed"),
        ("unexpected", 502, "guest_start_failed"),
        ("http", 418, "preserved HTTP error"),
        ("overload", 503, "database connection pool timed out"),
        ("gate", 409, "openclaw_update_in_progress"),
    ],
)
async def test_start_fault_preserves_typed_errors_and_maps_runtime_faults(surface, monkeypatch, fault, status, detail):
    from fastapi import HTTPException
    from pywhales.client_config import ParsingError
    from pywhales.exceptions import DockerException

    from app.db.errors import PoolCheckoutTimeoutError
    from app.main import database_overload, openclaw_update_gate
    from app.services.openclaw_update_gate import OpenClawUpdateGateError, OpenClawUpdateInProgressError

    app, runtime, agent = surface
    errors = {
        "parse": ParsingError("truncated inspect"),
        "io": OSError("daemon down"),
        "docker": DockerException(["docker", "inspect"], 1),
        "unexpected": ValueError("setup incomplete"),
        "http": HTTPException(418, "preserved HTTP error"),
        "overload": PoolCheckoutTimeoutError(),
        "gate": OpenClawUpdateInProgressError(),
    }
    app.add_exception_handler(PoolCheckoutTimeoutError, database_overload)
    app.add_exception_handler(OpenClawUpdateGateError, openclaw_update_gate)
    fail, reported = AsyncMock(), Mock()
    monkeypatch.setattr(lifecycle.start_store, "fail", fail)
    monkeypatch.setattr(lifecycle, "report_exception", reported)
    monkeypatch.setattr(lifecycle.agent_manager, "start_container", AsyncMock(side_effect=errors[fault]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/agents/{agent.id}/start")

    assert response.status_code == status
    payload = response.json()["detail"]
    assert (payload["code"] if isinstance(payload, dict) else payload) == detail
    assert runtime.committed == agent
    if fault in {"parse", "unexpected"}:
        reported.assert_called_once_with(errors[fault], source="runtime", operation="agent-lifecycle-start")
    else:
        reported.assert_not_called()
    if fault == "gate":
        fail.assert_not_awaited()
    if status == 503:
        assert response.headers["Retry-After"] == "5"


async def test_docker_unavailable_preserves_idle_http_contract(surface, monkeypatch):
    # Given the manager's deliberate Docker-unavailable mode.
    app, runtime, agent = surface
    monkeypatch.setattr(lifecycle.agent_manager, "docker_client", None)
    # When an explicit start is requested.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/agents/{agent.id}/start")
    # Then idle is a successful, durable result rather than a failed Docker start.
    assert response.status_code == 200
    assert response.json()["status"] == runtime.committed.status == "idle"
    release_at = runtime.events.index("release")
    assert runtime.events[release_at - 2 : release_at] == ["persist", "commit_locked"]
