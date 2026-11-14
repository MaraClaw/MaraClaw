"""Docker CLI readings for resource-pressure sampling: size/percent parsers and the batched inspect and stats calls."""

import json
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from pywhales import DockerClient

from app.services import agent_resource_pressure_docker as docker

GIB = 1024**3
FULL_A = "a" * 64
FULL_B = "b" * 64


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0B", 0),
        ("512B", 512),
        ("512kB", 512_000),
        ("2MB", 2_000_000),
        ("1KiB", 1024),
        ("10.5MiB", 11_010_048),
        ("1.5GiB", 3 * GIB // 2),
        ("  3 GiB ", 3 * GIB),
        ("1gib", GIB),
        ("4TiB", 4 * 1024**4),
        ("123", 123),
    ],
)
def test_size_strings_parse_to_bytes(text: str, expected: int):
    assert docker.parse_size_bytes(text) == expected


@pytest.mark.parametrize("text", ["", "--", "GiB", "12XB", "-5MiB", "1.5.2GiB", "1e3MiB", "1 / 2GiB", "nanMiB"])
def test_malformed_size_strings_are_unknown(text: str):
    assert docker.parse_size_bytes(text) is None


@pytest.mark.parametrize(("text", "expected"), [("12.34%", 12.34), ("0.00%", 0.0), ("250%", 250.0), (" 7 % ", 7.0)])
def test_percent_strings_parse(text: str, expected: float):
    assert docker.parse_percent(text) == expected


@pytest.mark.parametrize("text", ["", "--", "12", "-1%", "nan%", "inf%", "1.2.3%", "%"])
def test_malformed_percent_strings_are_unknown(text: str):
    assert docker.parse_percent(text) is None


@pytest.mark.parametrize(
    ("recorded", "reported", "same"),
    [
        (FULL_A, FULL_A, True),
        (FULL_A[:12], FULL_A, True),
        (FULL_A, FULL_A[:12], True),
        (FULL_A, FULL_B, False),
        ("", FULL_A, False),
        (FULL_A, "", False),
        (FULL_A.upper(), FULL_A, True),
    ],
)
def test_short_and_full_container_ids_name_one_container(recorded: str, reported: str, same: bool):
    assert docker.same_container(recorded, reported) is same


def _client() -> DockerClient:
    return cast("DockerClient", SimpleNamespace(docker_cmd=["docker", "--host", "unix:///x.sock"]))


def _cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    raises: Exception | None = None,
) -> list[tuple[list[str], dict[str, object]]]:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        if raises is not None:
            raise raises
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(docker.subprocess, "run", run)
    return calls


def _inspect_json(
    container_id: str = FULL_A,
    *,
    running: bool = True,
    restarting: bool = False,
    oom: bool = False,
    memory: int = 2 * GIB,
    nano_cpus: int = 1_000_000_000,
    finished: str = "0001-01-01T00:00:00Z",
    exit_code: int = 0,
    restarts: int = 0,
    owner: str | None = "agent-1",
    started: str | None = None,
) -> str:
    labels = {} if owner is None else {docker.AGENT_LABEL: owner}
    return json.dumps(
        {
            "Id": container_id,
            "State": {
                "Running": running,
                "Restarting": restarting,
                "OOMKilled": oom,
                "ExitCode": exit_code,
                "FinishedAt": finished,
                **({} if started is None else {"StartedAt": started}),
            },
            "HostConfig": {"Memory": memory, "NanoCpus": nano_cpus},
            "RestartCount": restarts,
            "Config": {"Labels": labels},
        }
    )


def test_inspect_runs_one_batched_cli_call_without_a_shell(monkeypatch: pytest.MonkeyPatch):
    calls = _cli(monkeypatch, stdout=_inspect_json(FULL_A) + "\n" + _inspect_json(FULL_B) + "\n")

    found = docker.inspect_guests(_client(), [FULL_A, FULL_B], 7.5)

    assert found is not None
    assert set(found) == {FULL_A, FULL_B}
    ((command, kwargs),) = calls
    assert command == [
        "docker",
        "--host",
        "unix:///x.sock",
        "container",
        "inspect",
        "--format",
        "{{json .}}",
        FULL_A,
        FULL_B,
    ]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["timeout"] == 7.5
    assert kwargs["check"] is False
    assert "shell" not in kwargs


def test_inspect_reads_state_limits_owner_and_the_nanosecond_finish_time(monkeypatch: pytest.MonkeyPatch):
    stdout = _inspect_json(
        oom=True,
        running=False,
        memory=3 * GIB,
        nano_cpus=1_500_000_000,
        finished="2026-11-13T05:04:03.123456789Z",
        exit_code=137,
        restarts=2,
    )
    _ = _cli(monkeypatch, stdout=stdout)

    found = docker.inspect_guests(_client(), [FULL_A[:12]], 5)

    assert found is not None
    guest = found[FULL_A[:12]]
    assert guest == docker.GuestInspection(
        container_id=FULL_A,
        owner="agent-1",
        running=False,
        memory_limit=3 * GIB,
        millicpus_limit=1500,
        oom_killed=True,
        finished_at=datetime(2026, 11, 13, 5, 4, 3, 123456, tzinfo=UTC),
        finished_at_raw="2026-11-13T05:04:03.123456789Z",
        exit_code=137,
        restart_count=2,
    )


@pytest.mark.parametrize(
    ("started", "expected"),
    [
        ("2026-11-13T18:02:43.21644151Z", datetime(2026, 11, 13, 18, 2, 43, 216441, tzinfo=UTC)),
        ("0001-01-01T00:00:00Z", None),
        ("not a time", None),
        (None, None),
    ],
    ids=["latest-start", "never-started", "garbage", "absent"],
)
def test_inspect_reads_the_latest_start_time(monkeypatch: pytest.MonkeyPatch, started: str | None, expected: object):
    _ = _cli(monkeypatch, stdout=_inspect_json(started=started))

    found = docker.inspect_guests(_client(), [FULL_A], 5)

    assert found is not None
    assert found[FULL_A].started_at == expected


@pytest.mark.parametrize(
    ("running", "restarting", "expected"), [(True, False, True), (False, False, False), (True, True, False)]
)
def test_a_restarting_guest_is_not_running(
    monkeypatch: pytest.MonkeyPatch, running: bool, restarting: bool, expected: bool
):
    _ = _cli(monkeypatch, stdout=_inspect_json(running=running, restarting=restarting))

    found = docker.inspect_guests(_client(), [FULL_A], 5)

    assert found is not None
    assert found[FULL_A].running is expected
    assert found[FULL_A].finished_at is None


def test_an_unlabelled_guest_has_no_owner(monkeypatch: pytest.MonkeyPatch):
    _ = _cli(monkeypatch, stdout=_inspect_json(owner=None))

    found = docker.inspect_guests(_client(), [FULL_A], 5)

    assert found is not None
    assert found[FULL_A].owner is None


def test_a_reference_docker_no_longer_knows_is_absent_while_the_others_are_read(monkeypatch: pytest.MonkeyPatch):
    _ = _cli(monkeypatch, stdout=_inspect_json(FULL_A), stderr="Error: No such container: gone\n", returncode=1)

    found = docker.inspect_guests(_client(), [FULL_A, "gone"], 5)

    assert found is not None
    assert set(found) == {FULL_A}


def test_every_reference_unknown_to_docker_is_an_answer_not_a_failure(monkeypatch: pytest.MonkeyPatch):
    _ = _cli(
        monkeypatch, stderr="Error response from daemon: No such container: a\nError: No such object: b\n", returncode=1
    )

    assert docker.inspect_guests(_client(), ["a", "b"], 5) == {}


@pytest.mark.parametrize(
    "stderr", ["", "Cannot connect to the Docker daemon at unix:///x.sock", "No such container: a\npermission denied"]
)
def test_a_failed_inspect_without_an_answer_is_unknown(monkeypatch: pytest.MonkeyPatch, stderr: str):
    _ = _cli(monkeypatch, stderr=stderr, returncode=1)

    assert docker.inspect_guests(_client(), [FULL_A], 5) is None


@pytest.mark.parametrize(
    "error", [subprocess.TimeoutExpired("docker", 5), FileNotFoundError("docker"), PermissionError()]
)
def test_inspect_is_unknown_when_the_cli_cannot_answer(monkeypatch: pytest.MonkeyPatch, error: Exception):
    _ = _cli(monkeypatch, raises=error)

    assert docker.inspect_guests(_client(), [FULL_A], 5) is None


def test_inspect_skips_garbage_lines_and_launches_nothing_without_work_or_time(monkeypatch: pytest.MonkeyPatch):
    calls = _cli(monkeypatch, stdout="not json\n[1]\n" + _inspect_json(FULL_A))

    found = docker.inspect_guests(_client(), [FULL_A], 5)
    assert found is not None
    assert set(found) == {FULL_A}
    assert docker.inspect_guests(_client(), [], 5) == {}
    assert docker.inspect_guests(_client(), [FULL_A], 0) is None
    assert len(calls) == 1


def _stats_line(container_id: str, memory: str = "10.5MiB / 2GiB", cpu: str = "12.50%") -> str:
    return json.dumps({"ID": container_id[:12], "Container": container_id[:12], "MemUsage": memory, "CPUPerc": cpu})


def test_stats_runs_one_non_streaming_batch_and_maps_readings_to_the_ids_asked_for(monkeypatch: pytest.MonkeyPatch):
    calls = _cli(
        monkeypatch,
        stdout=_stats_line(FULL_A, "1.5GiB / 2GiB", "95.00%") + "\n" + _stats_line(FULL_B, "--", "--") + "\n",
    )

    found = docker.guest_stats(_client(), [FULL_A, FULL_B], 9)

    assert found == {
        FULL_A: docker.GuestStats(FULL_A, 3 * GIB // 2, 95.0),
        FULL_B: docker.GuestStats(FULL_B, None, None),
    }
    ((command, kwargs),) = calls
    assert command == [
        "docker",
        "--host",
        "unix:///x.sock",
        "stats",
        "--no-stream",
        "--format",
        "{{json .}}",
        FULL_A,
        FULL_B,
    ]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["timeout"] == 9
    assert kwargs["check"] is False


def test_stats_leaves_out_a_container_without_a_reading(monkeypatch: pytest.MonkeyPatch):
    _ = _cli(monkeypatch, stdout=_stats_line(FULL_A), returncode=1)

    found = docker.guest_stats(_client(), [FULL_A, FULL_B], 5)

    assert found is not None
    assert set(found) == {FULL_A}


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("docker", 5), FileNotFoundError("docker")])
def test_stats_is_unknown_when_the_cli_cannot_answer(monkeypatch: pytest.MonkeyPatch, error: Exception):
    _ = _cli(monkeypatch, raises=error)

    assert docker.guest_stats(_client(), [FULL_A], 5) is None


def test_stats_without_any_reading_after_a_failed_exit_is_unknown_and_empty_input_launches_nothing(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = _cli(monkeypatch, returncode=1, stderr="Cannot connect to the Docker daemon")

    assert docker.guest_stats(_client(), [FULL_A], 5) is None
    assert docker.guest_stats(_client(), [], 5) == {}
    assert len(calls) == 1
