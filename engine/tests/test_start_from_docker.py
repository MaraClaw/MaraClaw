import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "start-from-docker.sh"
ENTRYPOINT = Path(__file__).resolve().parents[1] / "entrypoint.sh"
DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


def _launcher_with(tmp_path: Path, fake_docker: str) -> Path:
    directory = tmp_path / "engine checkout"
    directory.mkdir()
    script = directory / SCRIPT.name
    script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
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


@pytest.fixture
def run_launcher(tmp_path: Path) -> Path:
    """A launcher whose fake engine records every call and ``cp``s out a file naming the image given to ``create``."""
    return _launcher_with(
        tmp_path,
        "#!/bin/bash\n"
        "set -eu\n"
        'printf \'%s\\x1f\' "$@" >> "$QA_OUTPUT/calls"\n'
        "printf '\\n' >> \"$QA_OUTPUT/calls\"\n"
        'case "$1" in\n'
        '    create) for image; do :; done; printf "%s" "$image" > "$QA_OUTPUT/created" ;;\n'
        '    cp) cp "$QA_OUTPUT/created" "$3" ;;\n'
        "esac\n",
    )


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
