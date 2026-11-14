"""Batched, read-only Docker CLI readings of guest containers for resource-pressure sampling.

pywhales has no ``stats`` or ``--format`` passthrough for these, so like ``guest_probe`` and
``agent_workspace_quota.measure_layer_bytes`` this runs the local CLI itself: no shell, no stdin, one subprocess per
batch, and the child is killed and reaped on timeout. Callers run these in a worker thread, never inside a database
connection.
"""

import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from pywhales import ClientNotFoundError, DockerClient

from app.core.json_types import json_loads_value, object_mapping_from
from app.services.guest_identity import AGENT_LABEL

DOCKER_TIMEOUT_SECONDS: Final = 60.0
_SIZE: Final = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*$")
_PERCENT: Final = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")
_UNIT_BYTES: Final = {
    "": 1,
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "pb": 1000**5,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
    "pib": 1024**5,
}
_NANO_CPUS_PER_MILLICPU: Final = 1_000_000


def parse_size_bytes(text: str) -> int | None:
    """Bytes in a docker size string such as ``512kB``, ``1.5GiB`` or ``0B``; ``None`` for anything else (``--``)."""
    match = _SIZE.fullmatch(text)
    if match is None:
        return None
    factor = _UNIT_BYTES.get(match.group(2).lower())
    if factor is None:
        return None
    return int((Decimal(match.group(1)) * factor).to_integral_value())


def parse_percent(text: str) -> float | None:
    """Value of a docker percentage such as ``12.34%``; ``None`` for anything else (``--``)."""
    match = _PERCENT.fullmatch(text)
    return None if match is None else float(match.group(1))


def same_container(recorded: str, reported: str) -> bool:
    """Whether two container ids name one container (one may be the short form of the other)."""
    first, second = recorded.lower(), reported.lower()
    return bool(first) and bool(second) and (first.startswith(second) or second.startswith(first))


@dataclass(frozen=True, slots=True)
class GuestInspection:
    container_id: str
    owner: str | None
    running: bool
    memory_limit: int
    millicpus_limit: int
    oom_killed: bool
    finished_at: datetime | None
    finished_at_raw: str
    exit_code: int
    restart_count: int
    started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GuestStats:
    container_id: str
    memory_usage_bytes: int | None
    cpu_percent: float | None


@dataclass(frozen=True, slots=True)
class _CliOutput:
    returncode: int
    stdout: str
    stderr: str


def _run(client: DockerClient, arguments: list[str], timeout: float) -> _CliOutput | None:
    if timeout <= 0:
        return None
    try:
        command = [*(str(argument) for argument in client.docker_cmd), *arguments]
        result = subprocess.run(  # noqa: S603 - trusted SDK prefix, fixed verbs and recorded container ids, no shell.
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except ClientNotFoundError, OSError, subprocess.TimeoutExpired:
        return None
    return _CliOutput(result.returncode, result.stdout, result.stderr)


def _json_lines(output: str) -> list[dict[str, object]]:
    objects: list[dict[str, object]] = []
    for line in output.splitlines():
        try:
            parsed = json_loads_value(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            objects.append(object_mapping_from(parsed))
    return objects


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed if parsed.year > 1970 else None


def _inspection(item: dict[str, object]) -> GuestInspection:
    state = object_mapping_from(item.get("State"))
    host_config = object_mapping_from(item.get("HostConfig"))
    labels = object_mapping_from(object_mapping_from(item.get("Config")).get("Labels"))
    owner = labels.get(AGENT_LABEL)
    finished_raw = state.get("FinishedAt")
    raw = finished_raw if isinstance(finished_raw, str) else ""
    return GuestInspection(
        container_id=str(item.get("Id", "")),
        owner=owner if isinstance(owner, str) else None,
        running=state.get("Running") is True and state.get("Restarting") is not True,
        memory_limit=_integer(host_config.get("Memory")),
        millicpus_limit=_integer(host_config.get("NanoCpus")) // _NANO_CPUS_PER_MILLICPU,
        oom_killed=state.get("OOMKilled") is True,
        finished_at=_timestamp(raw),
        finished_at_raw=raw,
        exit_code=_integer(state.get("ExitCode")),
        restart_count=_integer(item.get("RestartCount")),
        started_at=_timestamp(state.get("StartedAt")),
    )


def inspect_guests(client: DockerClient, references: list[str], timeout: float) -> dict[str, GuestInspection] | None:
    """One ``container inspect`` for every reference, keyed by the reference asked for.

    A reference Docker no longer knows is simply absent. ``None`` means Docker could not answer at all (CLI missing,
    timeout, daemon down), so callers must neither sample nor close anything on it.
    """
    if not references:
        return {}
    output = _run(client, ["container", "inspect", "--format", "{{json .}}", *references], timeout)
    if output is None:
        return None
    inspections = [_inspection(item) for item in _json_lines(output.stdout)]
    if output.returncode != 0 and not inspections:
        problems = [line for line in output.stderr.splitlines() if line.strip()]
        if not problems or any("no such" not in line.lower() for line in problems):
            return None
    found: dict[str, GuestInspection] = {}
    for reference in references:
        match = next((item for item in inspections if same_container(reference, item.container_id)), None)
        if match is not None:
            found[reference] = match
    return found


def guest_stats(client: DockerClient, container_ids: list[str], timeout: float) -> dict[str, GuestStats] | None:
    """One non-streaming ``stats`` for the running containers, keyed by the id asked for; ``None`` when unanswered."""
    if not container_ids:
        return {}
    output = _run(client, ["stats", "--no-stream", "--format", "{{json .}}", *container_ids], timeout)
    if output is None:
        return None
    readings = _json_lines(output.stdout)
    if output.returncode != 0 and not readings:
        return None
    found: dict[str, GuestStats] = {}
    for container_id in container_ids:
        for reading in readings:
            reported = str(reading.get("ID") or reading.get("Container") or "")
            if not same_container(container_id, reported):
                continue
            usage = str(reading.get("MemUsage", "")).split("/", 1)[0]
            found[container_id] = GuestStats(
                container_id=container_id,
                memory_usage_bytes=parse_size_bytes(usage),
                cpu_percent=parse_percent(str(reading.get("CPUPerc", ""))),
            )
            break
    return found
