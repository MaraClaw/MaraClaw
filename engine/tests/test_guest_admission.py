import multiprocessing
import sys
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from uuid import uuid4

import anyio
import pytest
from pydantic import ValidationError
from python_on_whales import DockerClient
from python_on_whales.components.container import cli_wrapper as container_cli
from python_on_whales.exceptions import DockerException
from tests.test_guest_start_safety import Docker, env

__all__ = ["env"]

from app.config import Settings
from app.records.agent import AgentRecord
from app.services import guest_admission as module
from app.services.guest_admission import GuestAdmissionError, GuestStartPreflightError, guest_admission
from app.services.guest_identity import AGENT_LABEL


def test_admission_emits_separate_status_filters_with_real_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given the installed Docker SDK with only its process execution boundary stubbed.
    agent = AgentRecord(id=uuid4(), name="guest", creator_id=uuid4())
    client = DockerClient(client_call=[sys.executable])
    commands: list[list[str]] = []

    def run(command: Sequence[str | Path]) -> str:
        commands.append([str(argument) for argument in command])
        return ""

    monkeypatch.setattr(container_cli, "run", run)
    monkeypatch.setattr(module.settings, "DOCKER_NETWORK", "admission-deployment")
    monkeypatch.setattr(module.settings, "OPENCLAW_GUEST_MEMORY_LIMIT_BYTES", 20)
    monkeypatch.setattr(module.settings, "OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES", 40)
    # When admission inventories guests through the real ContainerCLI.list implementation.
    module._check_capacity(client, agent)
    # Then Docker receives distinct status filters alongside the label and deployment network.
    (command,) = commands
    filters = [value for flag, value in pairwise(command) if flag == "--filter"]
    assert sorted(filters) == sorted(
        [f"label={AGENT_LABEL}", "network=admission-deployment", "status=running", "status=restarting"]
    )
    assert "--all" in command


@pytest.mark.parametrize(("memory", "status"), [(21, "running"), (21, "restarting"), (0, "running")])
async def test_capacity_refusal_preserves_old_runtime(env, monkeypatch, memory, status):
    # Given a deployment whose other guest exceeds the remaining capacity or is unlimited.
    manager, docker, agent = env
    monkeypatch.setattr(module.settings, "OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES", 40)
    other = replace(agent, id=uuid4(), container_id="other")
    docker.add(other, memory=memory, status=status)
    # When start admission is refused.
    with pytest.raises(GuestAdmissionError) as error:
        await manager.replace_container(agent)
    # Then no old runtime is changed, and inventory is explicitly deployment-network scoped.
    assert error.value.code == ("guest_memory_unbounded" if memory == 0 else "guest_memory_capacity_exceeded")
    assert (agent.status, agent.container_id, agent.container_port) == ("running", "old", 18888)
    assert docker.removed == docker.runs == []
    assert docker.listings == [
        [
            ("label", AGENT_LABEL),
            ("network", module.settings.DOCKER_NETWORK),
            ("status", "running"),
            ("status", "restarting"),
        ]
    ]


async def test_replacement_excludes_same_agent_and_counts_actual_limits(env, monkeypatch):
    # Given an unlimited old guest of this agent and one stopped unlimited guest.
    manager, docker, agent = env
    monkeypatch.setattr(module.settings, "OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES", 40)
    docker.guests["old"].host_config.memory = 0
    docker.add(replace(agent, id=uuid4(), container_id="stopped"), memory=0, status="exited")
    docker.add(replace(agent, id=uuid4(), container_id="other"), memory=20)
    # When replacing at exactly the aggregate limit.
    result = await manager.replace_container(agent)
    # Then only the other running guest's actual hard limit consumes capacity.
    assert result == "created-1"
    assert "other" in docker.guests


@pytest.mark.parametrize("aggregate", [-1, 19])
def test_settings_reject_invalid_aggregate_boundary(aggregate):
    # Given a configured per-guest hard limit.
    # When aggregate capacity is negative or below it.
    with pytest.raises(ValidationError):
        Settings(OPENCLAW_GUEST_MEMORY_LIMIT_BYTES=20, OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES=aggregate)
    # Then settings never reach the manager with an impossible budget.


@pytest.mark.parametrize("boundary", ["lock", "inventory"])
async def test_admission_preflight_errors_preserve_old_runtime(env, monkeypatch, boundary):
    # Given an unavailable admission lock or Docker inventory.
    manager, docker, agent = env
    monkeypatch.setattr(module.settings, "OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES", 40)

    @asynccontextmanager
    async def fail_lock(_directory):
        raise OSError("read-only lock directory")
        yield

    def fail_inventory(**_kwargs):
        raise DockerException(["docker", "ps"], 1, stderr=b"daemon unavailable")

    if boundary == "lock":
        monkeypatch.setattr(module, "setup_workspace_lock", fail_lock)
    else:
        monkeypatch.setattr(docker, "list", fail_inventory)
    # When admission cannot establish a safe capacity decision.
    with pytest.raises(GuestStartPreflightError):
        await manager.replace_container(agent)
    # Then the HTTP boundary can report failure without false runtime publication.
    assert (agent.status, agent.container_id, agent.container_port) == ("running", "old", 18888)
    assert docker.removed == docker.runs == []


def _admission_contender(root, connection):
    import fcntl

    from app.records.agent import AgentRecord

    module.settings.OPENCLAW_GUEST_MEMORY_LIMIT_BYTES = 20
    module.settings.OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES = 20
    original_flock = fcntl.flock
    reported = False

    def flock(fd, operation):
        nonlocal reported
        try:
            return original_flock(fd, operation)
        except BlockingIOError:
            if not reported:
                connection.send("blocked")
                reported = True
            raise

    async def contend():
        agent = AgentRecord(id=uuid4(), name="contender", creator_id=uuid4())
        client = DockerClient()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(fcntl, "flock", flock)
            patch.setattr(client.container, "list", Docker().list)
            async with guest_admission(client, agent, Path(root)):
                connection.send("entered")

    anyio.run(contend)
    connection.close()


async def test_aggregate_lock_serializes_real_processes(env, monkeypatch, tmp_path):
    # Given one admission section held until Docker replacement/run could complete.
    _, docker, agent = env
    monkeypatch.setattr(module.settings, "OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES", 40)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_admission_contender, args=(str(tmp_path), sender))
    try:
        async with guest_admission(docker, agent, tmp_path):
            process.start()
            sender.close()
            # When another process attempts admission, flock reports real contention.
            with anyio.fail_after(15):
                blocked = await anyio.to_thread.run_sync(receiver.recv, abandon_on_cancel=True)
            assert blocked == "blocked"
        # Then it enters only after the first section releases its admission lock.
        with anyio.fail_after(15):
            entered = await anyio.to_thread.run_sync(receiver.recv, abandon_on_cancel=True)
        assert entered == "entered"
        await anyio.to_thread.run_sync(process.join, 5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join()
        receiver.close()
