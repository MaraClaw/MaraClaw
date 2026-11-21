import re
import subprocess
from pathlib import Path

import pytest

from app.config import SHUTDOWN_KILL_MARGIN_SECONDS, Settings

SCRIPT = Path(__file__).resolve().parents[1] / "start-from-docker.sh"
ENTRYPOINT = Path(__file__).resolve().parents[1] / "entrypoint.sh"
DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"
DEFAULT_STOP_TIMEOUT = str(
    Settings.model_fields["SHUTDOWN_HTTP_GRACE_SECONDS"].default
    + Settings.model_fields["SHUTDOWN_CLEANUP_BUDGET_SECONDS"].default
    + SHUTDOWN_KILL_MARGIN_SECONDS
)


LAUNCHERS = {
    "engine": SCRIPT,
    "web-e": SCRIPT.parents[1] / "web-e" / SCRIPT.name,
    "web-a": SCRIPT.parents[1] / "web-a" / SCRIPT.name,
}


def _launcher_with(tmp_path: Path, fake_docker: str, source: Path = SCRIPT) -> Path:
    directory = tmp_path / "engine checkout"
    directory.mkdir()
    script = directory / source.name
    script.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    bin_dir = directory / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(fake_docker, encoding="utf-8")
    docker.chmod(0o755)
    return script


@pytest.fixture
def launcher(tmp_path: Path) -> Path:
    return _launcher_with(
        tmp_path,
        "#!/bin/bash\n"
        "set -eu\n"
        '[ "$1" = build ] || exit 99\n'
        'printf "%s\\0" "$@" > "$QA_OUTPUT/args"\n'
        'printf "%s\\0" "${QA_VALUE-unset}" "${CORS_ORIGINS-unset}" "${QA_EMPTY-unset}" '
        '> "$QA_OUTPUT/values"\n',
    )


RECORDING_DOCKER = (
    "#!/bin/bash\n"
    "set -eu\n"
    'printf \'%s\\x1f\' "$@" >> "$QA_OUTPUT/calls"\n'
    "printf '\\n' >> \"$QA_OUTPUT/calls\"\n"
    'case "$1" in\n'
    '    create) for image; do :; done; printf "%s" "$image" > "$QA_OUTPUT/created" ;;\n'
    '    cp) cp "$QA_OUTPUT/created" "$3" ;;\n'
    '    info) [ -z "${QA_INFO_FAILS-}" ] || exit 1; printf "%s\\n" "${QA_LOG_DRIVER-}" ;;\n'
    "esac\n"
)


@pytest.fixture
def run_launcher(tmp_path: Path) -> Path:
    """A launcher whose fake engine records every call and ``cp``s out a file naming the image given to ``create``."""
    return _launcher_with(tmp_path, RECORDING_DOCKER)


def _launch(launcher: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed Bash command runs only the tmp_path launcher fixture.
        ["/bin/bash", str(launcher), *args],
        cwd=launcher.parent.parent,
        env={
            "PATH": f"{launcher.parent / 'bin'}:/usr/bin:/bin",
            "HOME": str(launcher.parent),
            "QA_OUTPUT": str(launcher.parent),
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def _build(launcher: Path, env_file: str) -> subprocess.CompletedProcess[str]:
    return _launch(launcher, "--build-only", ENV_FILE=env_file)


def _calls(launcher: Path) -> list[list[str]]:
    log = launcher.parent / "calls"
    if not log.exists():
        return []
    return [line.split("\x1f")[:-1] for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
@pytest.mark.parametrize("filename", ["engine.env", "engine qa.env"])
def test_build_sources_env_path_and_preserves_values(launcher: Path, absolute: bool, filename: str) -> None:
    # Given a shell env file with quoted values and a fake Docker executable.
    env_path = launcher.parent / filename
    env_path.write_text(
        "IMAGE_NAME='maraclaw-engine:qa'\n"
        "QA_VALUE='space \"quote\" = $HOME'\n"
        'CORS_ORIGINS=\'["https://one.example", "https://two.example"]\'\n'
        "QA_EMPTY=''\n"
        "MARACLAW_PIP_INDEX_URL='https://packages.example/simple?label=two words'\n",
        encoding="utf-8",
    )

    # When the launcher builds from a different working directory.
    result = _build(launcher, str(env_path) if absolute else filename)

    # Then Docker receives the sourced exports and each build argument intact.
    assert result.returncode == 0, result.stdout + result.stderr
    assert (launcher.parent / "args").read_bytes().split(b"\0") == [
        b"build",
        b"--build-arg",
        b"MARACLAW_PIP_INDEX_URL=https://packages.example/simple?label=two words",
        b"-t",
        b"maraclaw-engine:qa",
        b"-f",
        b"Dockerfile",
        b".",
        b"",
    ]
    assert (launcher.parent / "values").read_bytes().split(b"\0") == [
        b'space "quote" = $HOME',
        b'["https://one.example", "https://two.example"]',
        b"",
        b"",
    ]


@pytest.mark.parametrize("contents", ["", "# no settings\n", "HOME='/synthetic-home'\n"])
def test_build_accepts_env_without_forwardable_keys(launcher: Path, contents: str) -> None:
    # Given an env file with no application keys.
    (launcher.parent / "engine.env").write_text(contents, encoding="utf-8")

    # When the launcher builds using that relative env path.
    result = _build(launcher, "engine.env")

    # Then key collection does not abort before the build.
    assert result.returncode == 0, result.stdout + result.stderr
    assert (launcher.parent / "args").read_bytes().split(b"\0") == [
        b"build",
        b"-t",
        b"maraclaw-engine:local",
        b"-f",
        b"Dockerfile",
        b".",
        b"",
    ]


def test_docker_run_uses_narrow_bwrap_caps() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "--cap-add=ALL" not in source
    run_flags = {line.strip().rstrip("\\").strip() for line in source.splitlines()}
    assert "--privileged" not in run_flags
    for cap in ("SYS_ADMIN", "SETUID", "SETGID", "SYS_CHROOT", "SETPCAP", "NET_ADMIN", "SYS_PTRACE"):
        assert f"--cap-add={cap}" in source
    assert "--security-opt seccomp=unconfined" in source
    assert "sandbox escape can reach the host" in source


def test_docker_run_exposes_host_docker_for_agent_containers() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'network inspect "$DOCKER_NETWORK"' in source
    assert 'network create "$DOCKER_NETWORK"' in source
    assert '--network "$DOCKER_NETWORK"' in source
    assert "--network-alias maraclaw-engine" in source
    assert "/var/run/docker.sock" in source
    assert '-v "${DOCKER_SOCK}:/var/run/docker.sock"' in source
    assert '-v "${DATA_DIR}:${DATA_DIR}"' in source
    assert '-e "AGENT_DATA_DIR=${AGENT_DATA_DIR}"' in source
    assert '-e "STORAGE_LOCAL_ROOT=${AGENT_DATA_DIR}"' in source
    assert '-e "DOCKER_NETWORK=${DOCKER_NETWORK}"' in source
    assert '[ "$key" = "DOCKER_NETWORK" ] && continue' in source
    assert '[ "$key" = "STORAGE_LOCAL_ROOT" ] && continue' in source
    assert '-v "${DOCKER_CLI_HOST}:/usr/local/bin/docker:ro"' in source
    assert '-v "${SCRIPT_DIR}/entrypoint.sh:/app/entrypoint.sh:ro"' in source


def test_run_mounts_the_dockerfile_cli_fetched_once_per_image(run_launcher: Path) -> None:
    # Given a data directory still holding a CLI that an older launcher cached under a fixed name.
    stale = run_launcher.parent / ".docker-data" / ".bin" / "docker"
    stale.parent.mkdir(parents=True)
    _ = stale.write_text("docker:29.9.0-cli", encoding="utf-8")
    stale.chmod(0o755)
    dockerfile = re.search(r"^FROM (\S+) AS dockercli$", DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert dockerfile is not None
    other = "registry.example/docker:qa-cli"

    # When the launcher starts twice with its default CLI image, then once with another image.
    runs = [
        _launch(run_launcher, "--no-build"),
        _launch(run_launcher, "--no-build"),
        _launch(run_launcher, "--no-build", DOCKER_CLI_IMAGE=other),
    ]

    # Then the default is the Dockerfile's image, each image is fetched once, and each run mounts its own CLI.
    assert [run.returncode for run in runs] == [0, 0, 0], [run.stdout + run.stderr for run in runs]
    log = (run_launcher.parent / "calls").read_text(encoding="utf-8")
    calls = [line.split("\x1f")[:-1] for line in log.splitlines()]
    assert [call[-1] for call in calls if call[0] == "create"] == [dockerfile[1], other]
    mounted = [
        Path(arg.removesuffix(":/usr/local/bin/docker:ro")).read_text(encoding="utf-8")
        for call in calls
        if call[0] == "run"
        for arg in call
        if arg.endswith(":/usr/local/bin/docker:ro")
    ]
    assert mounted == [dockerfile[1], dockerfile[1], other]


def test_entrypoint_grants_app_user_docker_socket_group() -> None:
    source = ENTRYPOINT.read_text(encoding="utf-8")
    assert "/var/run/docker.sock" in source
    assert "usermod -aG" in source
    assert "gosu maraclaw" in source
    assert "chmod 666" not in source
    assert "do not chmod the host socket" in source


@pytest.mark.parametrize(
    ("contents", "stop_timeout"),
    [
        ("", DEFAULT_STOP_TIMEOUT),
        ("SHUTDOWN_HTTP_GRACE_SECONDS=20\nSHUTDOWN_CLEANUP_BUDGET_SECONDS=60\n", "90"),
        ("SHUTDOWN_HTTP_GRACE_SECONDS=20\nUVICORN_TIMEOUT_GRACEFUL_SHUTDOWN=25\n", "110"),
    ],
    ids=["defaults", "settings", "uvicorn override"],
)
def test_run_stop_timeout_covers_uvicorn_window_cleanup_budget_and_margin(
    run_launcher: Path, contents: str, stop_timeout: str
) -> None:
    # Given an env file that may set the uvicorn window and the cleanup budget.
    _ = (run_launcher.parent / "engine.env").write_text(contents, encoding="utf-8")

    # When the launcher starts the container.
    result = _launch(run_launcher, "--no-build", ENV_FILE="engine.env")

    # Then docker run allows the window, the budget and the margin between SIGTERM and SIGKILL.
    assert result.returncode == 0, result.stdout + result.stderr
    runs = [call for call in _calls(run_launcher) if call[0] == "run"]
    assert len(runs) == 1
    assert runs[0][runs[0].index("--stop-timeout") + 1] == stop_timeout
    assert DEFAULT_STOP_TIMEOUT == "115"


def test_run_refuses_a_shutdown_setting_that_is_not_whole_seconds(run_launcher: Path) -> None:
    # Given an env file whose cleanup budget is not a number of seconds.
    _ = (run_launcher.parent / "engine.env").write_text("SHUTDOWN_CLEANUP_BUDGET_SECONDS=soon\n", encoding="utf-8")

    # When the launcher starts.
    result = _launch(run_launcher, "--no-build", ENV_FILE="engine.env")

    # Then it fails before touching any container.
    assert result.returncode != 0
    assert "SHUTDOWN_CLEANUP_BUDGET_SECONDS" in result.stderr
    assert _calls(run_launcher) == []


ALL_LAUNCHERS = pytest.mark.parametrize("app", LAUNCHERS)
BOUND = ["max-size=10m", "max-file=3"]


@pytest.fixture
def log_launcher(tmp_path: Path, app: str) -> Path:
    return _launcher_with(tmp_path, RECORDING_DOCKER, LAUNCHERS[app])


def _docker_run(launcher: Path, *args: str, **env: str) -> list[str]:
    result = _launch(launcher, "--no-build", *args, **env)
    assert result.returncode == 0, result.stdout + result.stderr
    runs = [call for call in _calls(launcher) if call[0] == "run"]
    assert len(runs) == 1
    return runs[0]


def _log_opts(run: list[str]) -> list[str]:
    return [run[index + 1] for index, arg in enumerate(run) if arg == "--log-opt"]


@ALL_LAUNCHERS
@pytest.mark.parametrize("driver", ["json-file", "local"])
def test_run_bounds_logs_under_json_file_and_local(log_launcher: Path, driver: str) -> None:
    # Given a daemon whose default logging driver rotates by size.
    # When the launcher starts the container.
    run = _docker_run(log_launcher, QA_LOG_DRIVER=driver)

    # Then docker run carries the default size and file bound.
    assert _log_opts(run) == BOUND


@ALL_LAUNCHERS
@pytest.mark.parametrize("driver", ["journald", "awslogs", "syslog", "none", ""], ids=repr)
def test_run_passes_no_size_options_under_other_drivers(log_launcher: Path, driver: str) -> None:
    # Given a daemon whose driver rejects or ignores max-size, or reports no driver at all.
    # When the launcher starts the container.
    run = _docker_run(log_launcher, QA_LOG_DRIVER=driver)

    # Then docker run gets no log options.
    assert "--log-opt" not in run
    assert not any(arg.startswith(("max-size", "max-file")) for arg in run)


@ALL_LAUNCHERS
def test_run_passes_no_size_options_when_the_daemon_driver_cannot_be_read(log_launcher: Path) -> None:
    # Given an engine whose info command fails.
    # When the launcher starts the container.
    run = _docker_run(log_launcher, QA_INFO_FAILS="1")

    # Then an unknown driver is never given size options.
    assert "--log-opt" not in run


@ALL_LAUNCHERS
@pytest.mark.parametrize(
    ("daemon", "extra", "bounded"),
    [
        ("json-file", ["--log-driver", "journald"], False),
        ("json-file", ["--log-driver=awslogs"], False),
        ("json-file", ["--", "--log-driver", "journald"], False),
        ("json-file", ["--log-driver", "local", "--log-driver", "journald"], False),
        ("journald", ["--log-driver", "json-file"], True),
        ("journald", ["--log-driver=local"], True),
    ],
    ids=["separate", "joined", "after-separator", "last-wins", "json-file-over-journald", "local-over-journald"],
)
def test_run_follows_an_explicit_log_driver_over_the_daemon_default(
    log_launcher: Path, daemon: str, extra: list[str], bounded: bool
) -> None:
    # Given a daemon default and an operator who passes --log-driver through to docker run.
    # When the launcher starts the container.
    run = _docker_run(log_launcher, *extra, QA_LOG_DRIVER=daemon)

    # Then the explicit driver decides the bound and still reaches docker run, just before the image.
    passed = [arg for arg in extra if arg != "--"]
    assert run[-1 - len(passed) : -1] == passed
    assert _log_opts(run) == (BOUND if bounded else [])


@ALL_LAUNCHERS
def test_run_honors_the_log_bound_overrides(log_launcher: Path) -> None:
    # Given a size and a file count set in the environment.
    # When the launcher starts the container under json-file.
    run = _docker_run(log_launcher, QA_LOG_DRIVER="json-file", CONTAINER_LOG_MAX_SIZE="50m", CONTAINER_LOG_MAX_FILE="5")

    # Then docker run carries them in place of the defaults.
    assert _log_opts(run) == ["max-size=50m", "max-file=5"]


@ALL_LAUNCHERS
@pytest.mark.parametrize(("size", "count"), [("ten", "3"), ("0", "3"), ("10m", "0"), ("10m", "many")])
def test_run_refuses_an_unusable_log_bound_before_touching_any_container(
    log_launcher: Path, size: str, count: str
) -> None:
    # Given a size or a file count docker run would reject.
    # When the launcher starts.
    result = _launch(
        log_launcher,
        "--no-build",
        QA_LOG_DRIVER="json-file",
        CONTAINER_LOG_MAX_SIZE=size,
        CONTAINER_LOG_MAX_FILE=count,
    )

    # Then it fails before any container is removed or created.
    assert result.returncode != 0
    assert "CONTAINER_LOG_MAX_SIZE" in result.stderr
    assert _calls(log_launcher) == []
