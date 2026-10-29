"""Fake Docker CLI modelling the guest helper's AgentBootstrapCompleted check, and a fake helper executable."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from pywhales import DockerClient
from pywhales.exceptions import DockerException

HELPER = "/usr/local/bin/bootstrap-agent-container.py"
GUARD = ["python3", HELPER, "exec", "--"]
CHECK_READY = ["python3", HELPER, "check-ready"]
BOOTSTRAP_PENDING_EXIT = 75
PENDING_STDERR = "bootstrap-agent-container: bootstrap pending: event missing\n"


def guard_refusal() -> DockerException:
    """What ``docker exec`` raises when the helper refuses an action: its exit 75 and its stderr line."""
    return DockerException(["docker", "exec"], BOOTSTRAP_PENDING_EXIT, stderr=PENDING_STDERR.encode())


@dataclass(frozen=True, slots=True)
class BootCli:
    """A Docker CLI whose guests report readiness only when their event names their current boot."""

    executable: Path
    state: Path

    def client(self) -> DockerClient:
        return DockerClient(client_call=[str(self.executable)])

    def guest(self, reference: str, *, boot: str, event: str | None) -> None:
        """``event`` is the boot the finished event was written for; ``None`` means the file is missing."""
        (self.state / f"{reference}.json").write_text(json.dumps({"boot": boot, "event": event}), encoding="utf-8")

    def calls(self) -> list[list[str]]:
        log = self.state / "calls.jsonl"
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.is_file() else []


def boot_cli(directory: Path) -> BootCli:
    state = directory / "boot-state"
    state.mkdir()
    executable = directory / "boot-docker"
    executable.write_text(
        f"#!{sys.executable}\nimport json, pathlib, sys\n"
        f"state = pathlib.Path({json.dumps(str(state))})\n"
        "argv = sys.argv[1:]\n"
        "with (state / 'calls.jsonl').open('a') as log:\n"
        "    log.write(json.dumps(argv) + '\\n')\n"
        f"if argv[:1] != ['exec'] or argv[2:4] != ['python3', {HELPER!r}]:\n"
        "    sys.exit(99)\n"
        "record = state / (argv[1] + '.json')\n"
        "if record.is_file():\n"
        "    guest = json.loads(record.read_text())\n"
        "    if guest['event'] == guest['boot']:\n"
        "        sys.exit(0)\n"
        f"sys.stderr.write({PENDING_STDERR!r})\n"
        f"sys.exit({BOOTSTRAP_PENDING_EXIT})\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return BootCli(executable, state)


def fake_helper(directory: Path, *, finished: bool) -> Path:
    """Executable stand-in for the guest helper's ``exec --`` mode: exit 75 before the event, else run the action."""
    helper = directory / "bootstrap-agent-container.py"
    helper.write_text(
        "import os, sys\n"
        "if sys.argv[1:3] != ['exec', '--']:\n"
        "    sys.exit(64)\n"
        f"if not {finished!r}:\n"
        f"    sys.stderr.write({PENDING_STDERR!r})\n"
        f"    sys.exit({BOOTSTRAP_PENDING_EXIT})\n"
        "os.execvp(sys.argv[3], sys.argv[3:])\n",
        encoding="utf-8",
    )
    return helper
