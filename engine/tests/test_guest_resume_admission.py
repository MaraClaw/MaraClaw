"""Resumes reserve the target limits, then raise or lower the stopped guest to them, without replacing it."""

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
        (256, 640, "guest_memory_capacity_exceeded"),
        (128, 640, "guest_memory_capacity_exceeded"),
        (None, 1024, "guest_memory_unbounded"),
    ],
)
async def test_resume_refuses_target_limit_without_mutating_guest(env, monkeypatch, state, actual, aggregate, code):
    # Given a target of 512 but only 384 remaining, or a stopped guest with no recorded memory limit.
    manager, docker, agent = env
    guest = docker.guests["old"]
    guest.state.status = state
    guest.host_config.memory = actual
    agent.status = "stopped"
    docker.add(replace(agent, id=uuid4(), container_id="other"), memory=256)
    monkeypatch.setattr(guest_admission.settings, "OPENCLAW_GUEST_MEMORY_LIMIT_BYTES", 512)
    monkeypatch.setattr(guest_admission.settings, "OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES", aggregate)
    config = manager._agent_dir(agent.id) / "openclaw.json"
    config.write_text("retained config", encoding="utf-8")
    # When Start tries to resume the existing guest.
    with pytest.raises(GuestAdmissionError) as refused:
        await manager.start_container(agent)
    # Then admission refuses on the target (never the smaller inspected limit) before any update, start or write.
    assert refused.value.code == code
    assert (agent.container_id, agent.status, agent.container_port) == ("old", "stopped", 18888)
    assert (guest.state.status, guest.host_config.memory) == (state, actual)
    assert docker.updates == docker.starts == docker.runs == docker.removed == []
    assert config.read_text(encoding="utf-8") == "retained config"


@pytest.mark.parametrize("state", ["exited", "created"])
@pytest.mark.parametrize("actual", [256, 1024, 0], ids=["raised", "lowered", "unlimited"])
async def test_resume_updates_guest_to_target_at_exact_capacity_before_start(env, monkeypatch, state, actual):
    # Given a stopped guest whose limit differs from the 512 target, and exactly enough capacity for the target.
    manager, docker, agent = env
    guest = docker.guests["old"]
    guest.state.status = state
    guest.host_config.memory = guest.host_config.memory_swap = actual
    docker.add(replace(agent, id=uuid4(), container_id="other"), memory=256)
    monkeypatch.setattr(guest_admission.settings, "OPENCLAW_GUEST_MEMORY_LIMIT_BYTES", 512)
    monkeypatch.setattr(guest_admission.settings, "OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES", 768)
    start = docker.start

    def ordered_start(reference):
        docker.events.append("start")
        start(reference)

    monkeypatch.setattr(docker, "start", ordered_start)
    # When Start resumes it.
    result = await manager.start_container(agent)
    # Then admission inventories first, memory and swap move together to the target, and only then the same guest starts.
    assert result == "old"
    assert docker.events == ["list", "update", "start"]
    assert docker.updates == [
        ("old", {"memory": 512, "memory_swap": 512, "cpus": 1.0, "pids_limit": 512, "cpu_shares": 1024})
    ]
    assert (guest.host_config.memory, guest.host_config.memory_swap) == (512, 512)
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
