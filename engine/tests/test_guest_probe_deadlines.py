"""Readiness deadlines apply to completed probes, not just the loop entry."""

import json
import os
import sys
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter
from pywhales import DockerClient

from app.services import guest_readiness, openclaw_update_executor
from app.services.guest_admission import GuestStartPreflightError
from app.services.guest_probe import probe_guest
from app.services.openclaw_update_docker import UpgradeDocker
from test_openclaw_update_executor import Harness


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

    def ready(_client, reference, timeout):
        nonlocal now
        calls.append((reference, timeout))
        now = 1.0 if len(calls) == 1 else completion
        return True

    async def sleep(_delay):
        return None

    monkeypatch.setattr(guest_readiness, "monotonic", lambda: now)
    monkeypatch.setattr(guest_readiness, "probe_guest", ready, raising=False)
    monkeypatch.setattr(guest_readiness, "sleep", sleep)
    # When readiness completes its second successful command.
    result = await guest_readiness.guest_ready(client, "guest-id")
    # Then the probe receives only the remaining budget and late success is refused.
    assert result is False
    assert calls == [("guest-id", 120.0), ("guest-id", 119.0)]


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
def test_probe_timeout_kills_and_reaps_real_cli(tmp_path, upgrade):
    # Given a controlled executable sleeps much longer than the host probe budget.
    executable = tmp_path / "sleeping-docker"
    pid_file = tmp_path / "probe.pid"
    executable.write_text(
        f"#!{sys.executable}\nimport os, pathlib, time\n"
        f"pathlib.Path({json.dumps(str(pid_file))}).write_text(str(os.getpid()))\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    # When the real read-only helper times it out.
    client = DockerClient(client_call=[str(executable)])
    result = UpgradeDocker(client).ready("owned-guest", 1.0) if upgrade else probe_guest(client, "owned-guest", 1.0)
    # Then the timeout is failure and the direct child has already been waited for, not left a zombie.
    assert result is False
    pid = int(pid_file.read_text(encoding="utf-8"))
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
