"""Guest-visible writes, lifecycle changes and reconciliation versus the exclusive cutover lock."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest
from fastapi import HTTPException

from app.api import agents as agents_api, files as files_api
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.services import agent_stops, openclaw_update_exclusion as exclusion
from app.services.agent_setup_lock import WorkspaceLockTimeoutError, setup_workspace_lock
from app.services.openclaw_update_gate import OpenClawUpdateInProgressError


class Gate:
    """Stand-in for the run table: ``active`` flips when a run is created."""

    def __init__(self) -> None:
        self.active: bool = False
        self.checks: int = 0

    async def __call__(self, _agent_id: uuid.UUID) -> None:
        self.checks += 1
        if self.active:
            raise OpenClawUpdateInProgressError


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Gate:
    state = Gate()
    monkeypatch.setattr(exclusion, "assert_no_active_run", state)
    monkeypatch.setattr(exclusion.agent_manager, "_agent_dir", lambda agent_id: tmp_path / str(agent_id))
    monkeypatch.setattr(agent_stops.agent_manager, "_agent_dir", lambda agent_id: tmp_path / str(agent_id))
    return state


async def _settled() -> None:
    await asyncio.sleep(0.2)


async def test_write_is_rejected_immediately_while_a_run_is_active(gate: Gate):
    gate.active = True
    entered = False
    with pytest.raises(OpenClawUpdateInProgressError):
        async with exclusion.guest_write_section(uuid.uuid4()):
            entered = True
    assert not entered
    assert gate.checks == 1


async def test_contended_shared_write_times_out_without_entering_or_leaking_the_lock(gate, tmp_path, monkeypatch):
    # Given an exclusive holder and a zero-budget shared acquisition.
    agent_id = uuid.uuid4()
    monkeypatch.setattr(exclusion, "GUEST_WRITE_LOCK_TIMEOUT_SECONDS", 0)
    async with setup_workspace_lock(tmp_path / str(agent_id)):
        # When a guest writer attempts to enter.
        with pytest.raises(WorkspaceLockTimeoutError):
            async with exclusion.guest_write_section(agent_id):
                pytest.fail("a contended writer must not enter")
    # Then the failed acquisition left no held shared descriptor or locked recheck.
    assert gate.checks == 1
    async with setup_workspace_lock(tmp_path / str(agent_id), timeout_seconds=0):
        pass


async def test_shared_wait_budget_does_not_limit_the_acquired_write(gate, monkeypatch):
    # Given an available lock with no acquisition wait budget.
    monkeypatch.setattr(exclusion, "GUEST_WRITE_LOCK_TIMEOUT_SECONDS", 0)
    # When the acquired write yields to the event loop.
    async with exclusion.guest_write_section(uuid.uuid4()):
        await anyio.lowlevel.checkpoint()
    # Then only acquisition was bounded; the body and gate recheck completed.
    assert gate.checks == 2


async def test_writer_queued_behind_the_cutover_lock_rechecks_and_never_writes_into_the_snapshot_window(
    gate: Gate, tmp_path: Path
):
    agent_id = uuid.uuid4()
    wrote: list[str] = []

    async def writer() -> None:
        async with exclusion.guest_write_section(agent_id):
            wrote.append("written")

    async with setup_workspace_lock(tmp_path / str(agent_id)):
        task = asyncio.create_task(writer())
        await _settled()
        assert not task.done(), "the writer waits for the exclusive cutover lock"
        gate.active = True
    with pytest.raises(OpenClawUpdateInProgressError):
        await asyncio.wait_for(task, 2)
    assert wrote == []
    assert gate.checks == 2


async def test_writer_that_waited_out_a_finished_cutover_then_writes(gate: Gate, tmp_path: Path):
    agent_id = uuid.uuid4()
    wrote: list[str] = []

    async def writer() -> None:
        async with exclusion.guest_write_section(agent_id):
            wrote.append("written")

    async with setup_workspace_lock(tmp_path / str(agent_id)):
        task = asyncio.create_task(writer())
        await _settled()
    await asyncio.wait_for(task, 2)
    assert wrote == ["written"]


async def test_cutover_waits_for_in_flight_writers_but_writers_do_not_block_each_other(gate: Gate, tmp_path: Path):
    agent_id = uuid.uuid4()
    acquired: list[str] = []

    async def cutover() -> None:
        async with setup_workspace_lock(tmp_path / str(agent_id)):
            acquired.append("cutover")

    async with exclusion.guest_write_section(agent_id), exclusion.guest_write_section(agent_id):
        task = asyncio.create_task(cutover())
        await _settled()
        assert acquired == [], "the exclusive holder waits for both shared writers"
    await asyncio.wait_for(task, 2)
    assert acquired == ["cutover"]


async def test_route_dependency_authorizes_first_then_holds_the_shared_lock_for_the_handler(
    gate: Gate, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    agent_id = uuid.uuid4()
    order: list[str] = []

    async def check(_user: UserRecord, _agent_id: uuid.UUID) -> tuple[None, str]:
        order.append("access")
        return None, "use"

    monkeypatch.setattr(exclusion, "check_agent_access", check)
    dependency = exclusion.guest_write_exclusion(agent_id, UserRecord(id=uuid.uuid4()))
    await anext(dependency)
    assert order == ["access"]
    lock_attempt = asyncio.create_task(_try_exclusive(tmp_path / str(agent_id)))
    await _settled()
    assert not lock_attempt.done()
    await dependency.aclose()
    assert await asyncio.wait_for(lock_attempt, 2)


async def _try_exclusive(path: Path) -> bool:
    async with setup_workspace_lock(path):
        return True


def test_every_guest_visible_file_mutator_carries_the_exclusion_and_reads_do_not():
    mutators = {
        ("POST", "/agents/{agent_id}/api-key"),
        ("PUT", "/agents/{agent_id}/files/content"),
        ("POST", "/agents/{agent_id}/files/restore"),
        ("DELETE", "/agents/{agent_id}/files/content"),
        ("POST", "/agents/{agent_id}/files/import-skill"),
        ("POST", "/agents/{agent_id}/files/import-from-clawhub"),
        ("POST", "/agents/{agent_id}/files/import-from-url"),
        ("POST", "/agents/{agent_id}/files/upload"),
    }
    guarded: set[tuple[str, str]] = set()
    unguarded: set[tuple[str, str]] = set()
    for router in (agents_api.router, files_api.router, files_api.upload_router):
        for route in router.routes:
            methods = getattr(route, "methods", None) or set()
            dependant = getattr(route, "dependant", None)
            path = getattr(route, "path", "")
            if dependant is None:
                continue
            has_gate = any(dep.call is exclusion.guest_write_exclusion for dep in dependant.dependencies)
            for method in methods:
                (guarded if has_gate else unguarded).add((method, path))
    assert mutators <= guarded
    assert guarded <= mutators
    assert ("GET", "/agents/{agent_id}/files/content") in unguarded
    assert ("GET", "/agents/{agent_id}/files/download") in unguarded
    assert ("POST", "/agents/{agent_id}/files/download-ticket") in unguarded


async def test_lifecycle_section_yields_the_row_reread_under_the_lock_and_rechecks_the_gate(
    gate: Gate, monkeypatch: pytest.MonkeyPatch
):
    stale = AgentRecord(id=uuid.uuid4(), name="A", creator_id=uuid.uuid4(), container_id="old")
    fresh = replace(stale, container_id="published-by-cutover")
    monkeypatch.setattr(exclusion.agent_dao, "get", AsyncMock(return_value=fresh))
    async with exclusion.locked_fresh_agent(stale.id) as row:
        assert row is fresh
    assert gate.checks == 2
    gate.active = True
    with pytest.raises(OpenClawUpdateInProgressError):
        async with exclusion.locked_fresh_agent(stale.id):
            pytest.fail("lifecycle changes must not run during an active upgrade")


async def test_lifecycle_section_404s_a_deleted_agent_under_the_lock(gate: Gate, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(exclusion.agent_dao, "get", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as raised:
        async with exclusion.locked_fresh_agent(uuid.uuid4()):
            pytest.fail("unreachable")
    assert raised.value.status_code == 404


@pytest.fixture
def lifecycle(gate: Gate, monkeypatch: pytest.MonkeyPatch):
    stale = AgentRecord(id=uuid.uuid4(), name="A", creator_id=uuid.uuid4(), container_id="old", status="running")
    fresh = replace(stale, container_id="published-by-cutover", openclaw_image_id="sha256:pinned")
    user = UserRecord(id=uuid.uuid4())
    persisted: list[AgentRecord] = []

    async def access(_user: UserRecord, _agent_id: uuid.UUID) -> tuple[AgentRecord, str]:
        return stale, "manage"

    async def persist(agent: AgentRecord) -> AgentRecord:
        persisted.append(agent)
        return agent

    monkeypatch.setattr(agents_api, "check_agent_access", access)
    monkeypatch.setattr(agents_api, "_persist_agent_runtime", persist)
    monkeypatch.setattr(agents_api, "_agent_to_out", AsyncMock(return_value="out"))
    monkeypatch.setattr(exclusion.agent_dao, "get", AsyncMock(return_value=fresh))
    start, stop = AsyncMock(return_value="cid"), AsyncMock(return_value=True)
    monkeypatch.setattr(agents_api.agent_manager, "start_container", start)
    monkeypatch.setattr(agents_api.agent_manager, "stop_container", stop)
    return SimpleNamespace(user=user, stale=stale, fresh=fresh, start=start, stop=stop, persisted=persisted, gate=gate)


async def test_start_and_stop_act_on_the_fresh_row_not_the_stale_authorization_snapshot(lifecycle):
    _ = await agents_api.start_agent(lifecycle.stale.id, lifecycle.user)
    _ = await agents_api.stop_agent(lifecycle.stale.id, lifecycle.user)
    lifecycle.start.assert_awaited_once_with(lifecycle.fresh)
    lifecycle.stop.assert_awaited_once_with(lifecycle.fresh)
    assert lifecycle.persisted == [lifecycle.fresh, lifecycle.fresh]


async def test_start_and_stop_are_refused_while_an_upgrade_is_active(lifecycle):
    lifecycle.gate.active = True
    with pytest.raises(OpenClawUpdateInProgressError):
        _ = await agents_api.start_agent(lifecycle.stale.id, lifecycle.user)
    with pytest.raises(OpenClawUpdateInProgressError):
        _ = await agents_api.stop_agent(lifecycle.stale.id, lifecycle.user)
    lifecycle.start.assert_not_awaited()
    lifecycle.stop.assert_not_awaited()
    assert lifecycle.persisted == []


async def test_reconciliation_stop_waits_for_an_in_flight_cutover(gate: Gate, tmp_path: Path, monkeypatch):
    agent = AgentRecord(id=uuid.uuid4(), name="A", creator_id=uuid.uuid4(), container_id="c", status="stopped")
    current = AsyncMock(return_value=replace(agent))
    stop = AsyncMock(return_value=True)
    monkeypatch.setattr(agent_stops.agent_dao, "get", current)
    monkeypatch.setattr(agent_stops.agent_manager, "stop_container", stop)
    async with setup_workspace_lock(tmp_path / str(agent.id)):
        task = asyncio.create_task(agent_stops.stop_containers([agent]))
        await _settled()
        current.assert_not_awaited()
        stop.assert_not_awaited()
    await asyncio.wait_for(task, 2)
    current.assert_awaited_once_with(agent.id)
    stop.assert_awaited_once_with(agent)
