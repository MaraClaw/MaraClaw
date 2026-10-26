"""Resumes reserve actual inspected limits without replacing the existing guest."""

import fcntl
import threading
from dataclasses import replace
from uuid import uuid4

import anyio
import pytest

from app.services import guest_admission
from app.services.agent_setup_lock import drain_on_cancel, setup_workspace_lock
from app.services.guest_admission import GuestAdmissionError
from test_guest_start_replay import env, ready_guest

__all__ = ["env", "ready_guest"]


@pytest.mark.parametrize("state", ["exited", "created"])
@pytest.mark.parametrize(
    ("actual", "aggregate", "code"),
    [
        (512, 640, "guest_memory_capacity_exceeded"),
        (0, 1024, "guest_memory_unbounded"),
        (None, 1024, "guest_memory_unbounded"),
    ],
)
async def test_resume_refuses_actual_limit_without_mutating_guest(env, monkeypatch, state, actual, aggregate, code):
    # Given a default of 256 but a stopped guest needing 512 and only 384 remaining, or an unlimited guest.
    manager, docker, agent = env
    guest = docker.guests["old"]
    guest.state.status = state
    guest.host_config.memory = actual
    agent.status = "stopped"
    docker.add(replace(agent, id=uuid4(), container_id="other"), memory=256)
    monkeypatch.setattr(guest_admission.settings, "OPENCLAW_GUEST_MEMORY_LIMIT_BYTES", 256)
    monkeypatch.setattr(guest_admission.settings, "OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES", aggregate)
    config = manager._agent_dir(agent.id) / "openclaw.json"
    config.write_text("retained config", encoding="utf-8")
    # When Start tries to resume the existing guest.
    with pytest.raises(GuestAdmissionError) as refused:
        await manager.start_container(agent)
    # Then admission refuses before start/replacement/publication or any configuration write.
    assert refused.value.code == code
    assert (agent.container_id, agent.status, agent.container_port) == ("old", "stopped", 18888)
    assert (guest.state.status, guest.host_config.memory) == (state, actual)
    assert docker.starts == docker.runs == docker.removed == []
    assert config.read_text(encoding="utf-8") == "retained config"


@pytest.mark.parametrize("state", ["exited", "created"])
async def test_resume_uses_actual_limit_at_exact_capacity(env, monkeypatch, state):
    # Given a guest smaller than the current default, with exactly enough capacity for its real limit.
    manager, docker, agent = env
    docker.guests["old"].state.status = state
    docker.guests["old"].host_config.memory = 256
    docker.add(replace(agent, id=uuid4(), container_id="other"), memory=512)
    monkeypatch.setattr(guest_admission.settings, "OPENCLAW_GUEST_MEMORY_LIMIT_BYTES", 512)
    monkeypatch.setattr(guest_admission.settings, "OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES", 768)
    # When Start resumes it.
    result = await manager.start_container(agent)
    # Then the same guest starts and its hard limit is not silently changed to the configured default.
    assert result == "old"
    assert docker.guests["old"].host_config.memory == 256
    assert docker.starts == ["old"]
    assert docker.runs == docker.removed == []


async def test_running_reuse_does_not_request_new_admission(env, monkeypatch):
    # Given a running unlimited guest and an enabled aggregate budget.
    manager, docker, _agent = env
    docker.guests["old"].host_config.memory = 0
    monkeypatch.setattr(guest_admission.settings, "OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES", 40)
    # When Start reuses the already running guest.
    result = await manager.start_container(_agent)
    # Then readiness alone is sufficient; no new capacity is consumed.
    assert result == "old"
    assert docker.listings == docker.starts == []


async def test_creation_waits_for_resume_and_counts_its_limit(env, monkeypatch):
    # Given a resume blocked inside Docker start while the aggregate budget admits only one guest.
    manager, docker, agent = env
    directory = manager._agent_dir(agent.id)
    docker.guests["old"].state.status = "exited"
    monkeypatch.setattr(guest_admission.settings, "OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES", 20)
    entered, blocked = anyio.Event(), anyio.Event()
    release = threading.Event()
    flock = fcntl.flock
    start = docker.start
    refusals = []

    def wait_start(reference):
        anyio.from_thread.run_sync(entered.set)
        assert release.wait(5)
        start(reference)

    def observe_flock(lock, mode):
        try:
            return flock(lock, mode)
        except BlockingIOError:
            blocked.set()
            raise

    async def create():
        contender = replace(agent, id=uuid4(), container_id=None)
        try:
            await manager.replace_container(contender, create_only=True)
        except GuestAdmissionError as refusal:
            refusals.append(refusal.code)

    monkeypatch.setattr(docker, "start", wait_start)
    monkeypatch.setattr(fcntl, "flock", observe_flock)
    # When creation tries admission while the resume is still in flight.
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            try:
                group.start_soon(manager.start_container, agent)
                await entered.wait()
                group.start_soon(create)
                await blocked.wait()
            finally:
                release.set()
    # Then it only inventories after start completes and refuses instead of overcommitting capacity.
    assert refusals == ["guest_memory_capacity_exceeded"]
    assert docker.starts == ["old"]
    assert docker.runs == docker.removed == []
    assert (directory.parent / "_setup_locks" / "_guest_admission.lock").is_file()


async def test_cancelled_resume_drains_mutation_before_releasing_flocks(env, monkeypatch):
    # Given the production-style cancellation drain surrounds a workspace-locked resume.
    manager, docker, agent = env
    directory = manager._agent_dir(agent.id)
    docker.guests["old"].state.status = "exited"
    monkeypatch.setattr(guest_admission.settings, "OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES", 20)
    entered = anyio.Event()
    release, finished = threading.Event(), threading.Event()
    start = docker.start

    def wait_start(reference):
        anyio.from_thread.run_sync(entered.set)
        assert release.wait(5)
        start(reference)
        finished.set()

    async def section():
        async with setup_workspace_lock(directory):
            await manager.start_container(agent)

    async def run():
        await drain_on_cancel(section())

    monkeypatch.setattr(docker, "start", wait_start)
    # When cancellation arrives before the mutable Docker call returns.
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            try:
                group.start_soon(run)
                await entered.wait()
                group.cancel_scope.cancel()
                with anyio.CancelScope(shield=True):
                    await anyio.lowlevel.checkpoint()
                    for name in (str(agent.id), "_guest_admission"):
                        path = directory.parent / "_setup_locks" / f"{name}.lock"
                        with path.open("a+b") as lock, pytest.raises(BlockingIOError):
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    assert not finished.is_set()
            finally:
                release.set()
    # Then the child mutation was drained before the cancelled owner released either flock.
    assert finished.is_set()
    assert docker.starts == ["old"]
