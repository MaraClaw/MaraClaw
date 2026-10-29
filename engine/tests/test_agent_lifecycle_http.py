from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import anyio
import httpx
import pytest
from fastapi import Depends, FastAPI

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
from app.services.guest_admission import GuestAdmissionError, GuestStartPreflightError
from app.services.guest_identity import GuestOwnershipError


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

    monkeypatch.setattr(lifecycle.start_store, "begin", begin)
    monkeypatch.setattr(lifecycle.start_store, "cancel", cancel)
    monkeypatch.setattr(lifecycle.start_store, "publish", publish)
    monkeypatch.setattr(lifecycle.start_store, "reserve", reserve)
    monkeypatch.setattr(lifecycle.start_store, "fail", fail)

    async def authorize(_user, _id):
        return replace(runtime.committed), "manage"

    async def get_agent(_id):
        return replace(runtime.committed)

    async def gate(_id):
        return None

    @asynccontextmanager
    async def lock(directory, *, shared=False):
        async with setup_workspace_lock(directory, shared=shared):
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


@pytest.mark.parametrize("failure", ["admission", "preflight", "ownership", "unexpected"])
async def test_preflight_outcomes_do_not_persist_runtime(surface, monkeypatch, failure):
    # Given a failure before destructive replacement.
    app, runtime, agent = surface
    errors = {
        "admission": GuestAdmissionError("guest_memory_capacity_exceeded", "capacity exhausted"),
        "preflight": GuestStartPreflightError("preparation failed"),
        "ownership": GuestOwnershipError(agent.id, "old", str(uuid4())),
        "unexpected": RuntimeError("programming failure"),
    }

    async def fail(_record):
        raise errors[failure]

    monkeypatch.setattr(lifecycle.agent_manager, "start_container", fail)
    # When the request crosses the HTTP boundary.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        if failure == "unexpected":
            with pytest.raises(RuntimeError, match="programming failure"):
                await client.post(f"/api/agents/{agent.id}/start")
        else:
            response = await client.post(f"/api/agents/{agent.id}/start")
            assert response.status_code == (409 if failure == "admission" else 502)
    # Then expected refusals leave references intact; unexpected errors propagate.
    assert runtime.committed == agent
    assert "persist" not in runtime.events


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
