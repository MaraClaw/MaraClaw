"""Readiness deadlines apply to completed probes, not just the loop entry."""

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import suppress
from types import SimpleNamespace

import anyio
import anyio.lowlevel
import pytest
from pydantic import TypeAdapter
from pywhales import DockerClient, client_config
from pywhales.components.container import cli_wrapper as container_cli

from app.services import guest_readiness, openclaw_update_executor
from app.services.docker_capacity import docker_capacity, run_docker, shutdown_docker_capacity
from app.services.guest_admission import GuestStartPreflightError, GuestStartStoppedError
from app.services.guest_probe import ProbeChild, probe_guest
from app.services.openclaw_update_docker import UpgradeDocker
from test_guest_start_safety import BlockedProbe, env
from test_openclaw_update_executor import Harness

__all__ = ["env"]


def test_preflight_unchanged_callers_default_to_configuration_failure():
    assert GuestStartPreflightError("safe failure").code == "configuration_failed"


@pytest.mark.parametrize("completion", [120.0, 121.0])
async def test_upgrade_rejects_second_pass_at_or_after_deadline(tmp_path, monkeypatch, completion):
    # Given two successful probes, with the second completing at the overall deadline or later.
    harness = Harness(tmp_path)
    harness.executor.ready_timeout = 120.0
    now = 0.0
    calls = []

    def ready(reference, timeout=120.0):
        nonlocal now
        calls.append((reference, timeout))
        now = 1.0 if len(calls) == 1 else completion
        return True

    async def sleep(_delay):
        return None

    monkeypatch.setattr(
        type(harness.docker), "ready", lambda _docker, reference, timeout=120.0: ready(reference, timeout)
    )
    monkeypatch.setattr(openclaw_update_executor, "anyio", SimpleNamespace(current_time=lambda: now, sleep=sleep))
    # When readiness completes its second successful command.
    result = await harness.executor._ready("old-id")
    # Then an expired readiness budget cannot authorize publication.
    assert result is False
    assert calls == [("old-id", 120.0), ("old-id", 119.0)]


@pytest.mark.parametrize("completion", [120.0, 121.0])
async def test_ordinary_rejects_second_pass_at_or_after_deadline(monkeypatch, completion):
    # Given two successful probes, with the second completing at the overall deadline or later.
    client = DockerClient()
    now = 0.0
    calls = []

    def ready(_client, reference, timeout, _child):
        nonlocal now
        calls.append((reference, timeout))
        now = 115.0 if len(calls) == 1 else completion
        return True

    async def sleep(_delay):
        return None

    monkeypatch.setattr(guest_readiness, "monotonic", lambda: now)
    monkeypatch.setattr(guest_readiness, "probe_guest", ready, raising=False)
    monkeypatch.setattr(guest_readiness, "sleep", sleep)
    # When readiness completes its second successful command.
    result = await guest_readiness.guest_ready(client, "guest-id")
    # Then each probe gets its own timeout, cut to the remaining budget, and late success is refused.
    assert result is False
    assert calls == [("guest-id", guest_readiness.PROBE_TIMEOUT_SECONDS), ("guest-id", 5.0)]


@pytest.mark.parametrize("upgrade", [False, True])
async def test_readiness_caps_sleep_to_remaining_deadline(tmp_path, monkeypatch, upgrade):
    # Given a failing probe consumes all but one second of the readiness budget.
    now = 0.0
    sleeps = []

    def probe(*_args):
        nonlocal now
        now = 119.0
        return False

    async def sleep(delay):
        nonlocal now
        sleeps.append(delay)
        now += delay

    monkeypatch.setattr(guest_readiness, "monotonic", lambda: now)
    monkeypatch.setattr(guest_readiness, "probe_guest", probe)
    monkeypatch.setattr(
        guest_readiness, "observe_guest", lambda _client, _reference, restarts, _until: (restarts, None)
    )
    monkeypatch.setattr(guest_readiness, "sleep", sleep)
    harness = Harness(tmp_path)
    harness.executor.ready_timeout = 120.0
    harness.executor.ready_interval = 2.0
    monkeypatch.setattr(type(harness.docker), "ready", probe)
    monkeypatch.setattr(openclaw_update_executor, "anyio", SimpleNamespace(current_time=lambda: now, sleep=sleep))
    # When a normal polling interval would overrun the remaining budget.
    result = (
        await harness.executor._ready("old-id")
        if upgrade
        else await guest_readiness.guest_ready(DockerClient(), "guest")
    )
    # Then the sleep ends at the deadline, without starting another command.
    assert result is False
    assert sleeps == [1.0]


@pytest.mark.parametrize(
    ("fate", "probes"),
    [("exited", 1), ("removed", 1), ("crash-looping", 4), ("restarted-twice", 40), ("booting", 40)],
)
async def test_readiness_ends_early_only_when_docker_rules_the_guest_out(env, monkeypatch, fate, probes):
    # Given a guest that never passes its probe, and whose Docker state changes while readiness waits.
    _manager, docker, _agent = env
    now = 0.0
    calls = []

    def probe(_client, reference, _timeout, _child):
        nonlocal now
        now += 1.0
        calls.append(reference)
        if fate == "exited":
            docker.guests[reference].state.status = "exited"
        elif fate == "removed":
            docker.guests.pop(reference, None)
        elif fate == "crash-looping" or (fate == "restarted-twice" and len(calls) <= 3):
            docker.guests[reference].restart_count += 1
        return False

    async def sleep(delay):
        nonlocal now
        now += delay

    monkeypatch.setattr(guest_readiness, "monotonic", lambda: now)
    monkeypatch.setattr(guest_readiness, "probe_guest", probe)
    monkeypatch.setattr(guest_readiness, "sleep", sleep)
    # When readiness polls it against the 120-second deadline.
    result = await guest_readiness.guest_ready(docker, "old")
    # Then a stopped, removed or crash-looping guest ends the wait at once; anything else uses the whole budget.
    assert result is False
    assert len(calls) == probes


async def test_readiness_reports_a_stop_at_its_next_poll_apart_from_failure(monkeypatch):
    # Given a guest that is not ready, and an agent that stops qualifying after the first poll.
    probes = []

    def probe(_client, reference, _timeout, _child):
        probes.append(reference)
        return False

    async def halted():
        return "ineligible" if probes else None

    monkeypatch.setattr(guest_readiness, "probe_guest", probe)
    monkeypatch.setattr(
        guest_readiness, "observe_guest", lambda _client, _reference, restarts, _until: (restarts, None)
    )
    monkeypatch.setattr(guest_readiness, "READY_INTERVAL_SECONDS", 0)
    # When readiness polls under that checkpoint.
    with pytest.raises(GuestStartStoppedError) as stop:
        await guest_readiness.guest_ready(DockerClient(), "guest", halted=halted)
    # Then the stop ends the wait at the next poll with its reason, rather than as a failed or timed-out probe.
    assert stop.value.reason == "ineligible"
    assert probes == ["guest"]


async def test_readiness_stops_waiting_for_a_worker_thread_at_the_deadline(monkeypatch):
    # Given a guest whose probe fails at once and an observation that never gets a worker thread.
    dispatched = []

    async def run_docker(function, *arguments, abandon_on_cancel=False):
        if function is guest_readiness.observe_guest:
            dispatched.append(arguments)
            await anyio.sleep_forever()
        return False

    monkeypatch.setattr(guest_readiness, "READY_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(guest_readiness, "run_docker", run_docker)
    # When readiness runs against a short real deadline.
    started = time.monotonic()
    with anyio.fail_after(5):
        result = await guest_readiness.guest_ready(DockerClient(), "guest")
    elapsed = time.monotonic() - started
    # Then the wait for a worker thread ends at that deadline instead of outliving it.
    assert result is False
    assert dispatched
    assert elapsed < 1.0


async def test_readiness_returns_at_the_deadline_while_the_observe_thread_is_blocked(monkeypatch):
    # Given a failing probe and an observation whose real worker thread stays blocked until the test releases it.
    entered = threading.Event()
    release = threading.Event()
    finished = anyio.Event()
    outcome = []

    def observe(_client, _reference, restarts, _until):
        entered.set()
        release.wait(10)
        return restarts, None

    async def readiness():
        started = time.monotonic()
        result = await guest_readiness.guest_ready(DockerClient(), "guest")
        outcome.append((result, time.monotonic() - started))
        finished.set()

    monkeypatch.setattr(guest_readiness, "READY_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(guest_readiness, "probe_guest", lambda *_args: False)
    monkeypatch.setattr(guest_readiness, "observe_guest", observe)
    # When readiness runs against a short real deadline that passes while that thread is still blocked.
    async with anyio.create_task_group() as group:
        group.start_soon(readiness)
        try:
            with anyio.move_on_after(2):
                await finished.wait()
        finally:
            release.set()
    # Then readiness gave up at its own deadline instead of waiting for the thread to be released.
    assert entered.is_set()
    assert outcome
    assert outcome[0][0] is False
    assert outcome[0][1] < 1.5


async def test_observation_deadline_never_outlives_the_readiness_deadline(monkeypatch):
    # Given one clock that advances on every read, wherever readiness reads it, and a probe that always fails.
    now = 0.0
    timeout = 0.3
    untils = []

    def clock():
        nonlocal now
        reading, now = now, now + 0.02
        return reading

    async def run_docker(function, *arguments, abandon_on_cancel=False):
        return function(*arguments)

    async def sleep(_delay):
        return None

    def observe(_client, _reference, restarts, until):
        untils.append(until)
        return restarts, None

    deadline = now + timeout
    monkeypatch.setattr(guest_readiness, "READY_TIMEOUT_SECONDS", timeout)
    monkeypatch.setattr(guest_readiness, "monotonic", clock)
    monkeypatch.setattr(guest_readiness, "time", SimpleNamespace(monotonic=clock))
    monkeypatch.setattr(guest_readiness, "run_docker", run_docker)
    monkeypatch.setattr(guest_readiness, "probe_guest", lambda *_args: False)
    monkeypatch.setattr(guest_readiness, "observe_guest", observe)
    monkeypatch.setattr(guest_readiness, "sleep", sleep)
    # When readiness polls until its deadline, with each clock read taking a little time.
    result = await guest_readiness.guest_ready(DockerClient(), "guest")
    # Then no observation deadline falls after the readiness deadline, so a late worker thread launches nothing.
    assert result is False
    assert untils
    assert all(until <= deadline for until in untils)


@pytest.mark.parametrize(("offset", "launched"), [(-1.0, 0), (30.0, 1)])
def test_observation_after_its_deadline_launches_no_cli(monkeypatch, offset, launched):
    # Given the installed SDK with only its process boundary stubbed, and a deadline in the past or the future.
    client = DockerClient(client_call=[sys.executable])
    commands = []

    def run(command):
        commands.append([str(argument) for argument in command])
        return json.dumps([{"Id": "guest", "RestartCount": 0, "State": {"Status": "running"}}])

    monkeypatch.setattr(container_cli, "run", run)
    monkeypatch.setattr(client_config, "CACHE_VALIDITY_PERIOD", 3600.0)
    # When a worker thread that started late observes the guest.
    result = guest_readiness.observe_guest(client, "guest", 7, time.monotonic() + offset)
    # Then an expired deadline launches no command, and a live one launches exactly one.
    assert result == (7, None)
    assert len(commands) == launched


def test_probe_preserves_real_client_prefix_and_environment(tmp_path, monkeypatch):
    # Given a distinct executable and every public Docker connection option, plus inherited environment.
    executable = tmp_path / "owned-docker"
    receipt = tmp_path / "arguments.json"
    executable.write_text(
        f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['PROBE_RECEIPT']).write_text(json.dumps([*sys.argv[1:], os.environ['DOCKER_HOST']]))\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PROBE_RECEIPT", str(receipt))
    monkeypatch.setenv("DOCKER_HOST", "inherited-docker-host")
    client = DockerClient(
        client_call=[str(executable), "prefix-sentinel"],
        config=tmp_path / "config",
        context="context-sentinel",
        host="host-sentinel",
        debug=True,
        log_level="debug",
        tls=True,
        tlscacert="ca-sentinel",
        tlscert="cert-sentinel",
        tlskey="key-sentinel",
        tlsverify=True,
    )
    # When the real runner performs its read-only current-boot check.
    result = probe_guest(client, "owned-guest", 5.0)
    # Then connection selection, executable arguments and environment survive unchanged.
    assert result is True
    arguments = TypeAdapter(list[str]).validate_json(receipt.read_text(encoding="utf-8"))
    assert arguments[:-1] == [
        "prefix-sentinel",
        "--config",
        str(tmp_path / "config"),
        "--context",
        "context-sentinel",
        "--debug",
        "--host",
        "host-sentinel",
        "--log-level",
        "debug",
        "--tls",
        "--tlscacert",
        "ca-sentinel",
        "--tlscert",
        "cert-sentinel",
        "--tlskey",
        "key-sentinel",
        "--tlsverify",
        "exec",
        "owned-guest",
        "python3",
        "/usr/local/bin/bootstrap-agent-container.py",
        "check-ready",
    ]
    assert arguments[-1] == "inherited-docker-host"


@pytest.mark.parametrize("upgrade", [False, True])
def test_probe_timeout_kills_and_reaps_real_cli(tmp_path, monkeypatch, upgrade):
    # Given a controlled executable sleeps much longer than the host probe budget.
    executable = tmp_path / "sleeping-docker"
    executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n", encoding="utf-8")
    executable.chmod(0o700)
    # The pid comes from the spawn itself: under load the deadline can kill the interpreter before it runs any code.
    spawned: list[int] = []
    spawn = ProbeChild.spawn

    def recording_spawn(child: ProbeChild, command: list[str]) -> subprocess.Popen[bytes] | None:
        process = spawn(child, command)
        if process is not None:
            spawned.append(process.pid)
        return process

    monkeypatch.setattr(ProbeChild, "spawn", recording_spawn)
    # When the real read-only helper times it out.
    client = DockerClient(client_call=[str(executable)])
    result = UpgradeDocker(client).ready("owned-guest", 1.0) if upgrade else probe_guest(client, "owned-guest", 1.0)
    # Then the timeout is failure and the one child it spawned has already been waited for, not left a zombie.
    assert result is False
    assert len(spawned) == 1
    assert_reaped(spawned[0])


def assert_reaped(pid: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


@pytest.mark.parametrize("returncode", [0, 1])
def test_probe_reports_cli_exit_status(returncode):
    # Given an actual command whose exit status models a ready or unready guest.
    client = DockerClient(client_call=[sys.executable, "-c", f"raise SystemExit({returncode})"])
    # When the command finishes within budget.
    result = probe_guest(client, "owned-guest", 5.0)
    # Then only an exit-zero probe reports readiness.
    assert result is (returncode == 0)


async def test_cancelled_readiness_returns_while_its_probe_is_still_blocked(monkeypatch):
    # Given a probe whose lane thread stays blocked on a threading.Event until the test releases it.
    probe = BlockedProbe()
    returned: list[bool] = []

    async def readiness():
        try:
            await guest_readiness.guest_ready(DockerClient(), "guest")
        finally:
            returned.append(probe.finished.is_set())

    monkeypatch.setattr(guest_readiness, "probe_guest", probe)
    try:
        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(readiness)
                await probe.entered.wait()
                # When the start waiting for readiness is cancelled.
                group.cancel_scope.cancel()
    finally:
        probe.release.set()
    # Then readiness gave control back before the probe's thread returned.
    assert returned == [False]


async def test_readiness_ends_at_a_stop_reported_while_its_probe_is_still_blocked(monkeypatch):
    # Given a blocked probe, and a stop that the first poll made while it runs reports.
    probe = BlockedProbe()
    polls = 0

    async def halted():
        nonlocal polls
        polls += 1
        if polls == 1:
            return None
        await probe.entered.wait()
        return "cancelled"

    monkeypatch.setattr(guest_readiness, "probe_guest", probe)
    monkeypatch.setattr(guest_readiness, "HALT_POLL_SECONDS", 0)
    try:
        # When readiness polls under that checkpoint.
        with anyio.fail_after(5), pytest.raises(GuestStartStoppedError) as stop:
            await guest_readiness.guest_ready(DockerClient(), "guest", halted=halted)
        # Then the stop ends readiness with its reason while the probe's thread is still blocked.
        assert stop.value.reason == "cancelled"
        assert not probe.finished.is_set()
        assert polls == 2
    finally:
        probe.release.set()


@pytest.mark.parametrize("ending", ["cancelled", "halted"])
async def test_ended_readiness_kills_and_reaps_the_probe_cli(tmp_path, monkeypatch, ending):
    # Given a real CLI that reports its pid through a FIFO, then sleeps far past the probe timeout.
    fifo = tmp_path / "probe.fifo"
    os.mkfifo(fifo)
    executable = tmp_path / "hung-docker"
    executable.write_text(
        f"#!{sys.executable}\nimport os, pathlib, time\n"
        f"pathlib.Path({json.dumps(str(fifo))}).write_text(str(os.getpid()))\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    client = DockerClient(client_call=[str(executable)])
    pids: list[int] = []
    polls = 0

    async def spawned():
        pids.append(int(await anyio.to_thread.run_sync(fifo.read_text, abandon_on_cancel=True)))

    async def halted():
        nonlocal polls
        polls += 1
        if polls == 1:
            return None
        await spawned()
        return "cancelled"

    monkeypatch.setattr(guest_readiness, "PROBE_TIMEOUT_SECONDS", 60.0)
    monkeypatch.setattr(guest_readiness, "HALT_POLL_SECONDS", 0)
    try:
        # When readiness is cancelled, or a stop is reported, while that CLI runs.
        with anyio.fail_after(10):
            if ending == "cancelled":
                async with anyio.create_task_group() as group:
                    group.start_soon(guest_readiness.guest_ready, client, "guest")
                    await spawned()
                    group.cancel_scope.cancel()
            else:
                with pytest.raises(GuestStartStoppedError):
                    await guest_readiness.guest_ready(client, "guest", halted=halted)
        executor = docker_capacity().executor
        assert executor is not None
        shutdown_docker_capacity()
        with anyio.fail_after(5):
            await anyio.to_thread.run_sync(executor.shutdown, abandon_on_cancel=True)
    finally:
        # A reader still waiting for a CLI that never started gets end-of-file instead of blocking forever.
        with suppress(OSError):
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
    # Then the probe's lane thread killed and reaped the CLI long before its 60-second timeout.
    assert_reaped(pids[0])


async def test_stop_queued_behind_probing_starts_gets_a_slot_within_one_probe_attempt(monkeypatch):
    # Given a two-slot lane whose slots run the first probe attempt of two starts, each blocked on an Event.
    docker_capacity().limit = 2
    token = anyio.lowlevel.current_token()
    attempts, order = 0, []
    both_probing, stopped, interval = anyio.Event(), anyio.Event(), anyio.Event()
    release, lock = threading.Event(), threading.Lock()

    def probe(_client, _reference, _timeout, _child):
        nonlocal attempts
        with lock:
            attempts += 1
            blocking = attempts <= 2
            order.append("probe")
            if attempts == 2:
                anyio.from_thread.run_sync(both_probing.set, token=token)
        if blocking:
            _ = release.wait(10)
        return not blocking

    def stop():
        order.append("stop")
        anyio.from_thread.run_sync(stopped.set, token=token)

    async def wait_between_attempts(_delay):
        await interval.wait()

    monkeypatch.setattr(guest_readiness, "probe_guest", probe)
    monkeypatch.setattr(
        guest_readiness, "observe_guest", lambda _client, _reference, restarts, _until: (restarts, None)
    )
    monkeypatch.setattr(guest_readiness, "sleep", wait_between_attempts)
    results = []

    async def start():
        results.append(await guest_readiness.guest_ready(DockerClient(), "guest"))

    try:
        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(start)
                group.start_soon(start)
                await both_probing.wait()
                # When a user's stop queues behind them and the first attempts end unready.
                group.start_soon(run_docker, stop)
                await anyio.lowlevel.checkpoint()
                release.set()
                # Then the stop runs before either start's next attempt: each attempt held a slot only while it ran.
                await stopped.wait()
                assert order == ["probe", "probe", "stop"]
                interval.set()
    finally:
        release.set()
        shutdown_docker_capacity()
    assert results == [True, True]
