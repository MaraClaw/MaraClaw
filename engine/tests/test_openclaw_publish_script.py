import os
import subprocess
from pathlib import Path

import pytest

from test_openclaw_build_script import GOGCLI_AMD64_SHA256, GOGCLI_ARM64_SHA256, install_fake_docker

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLISH_SCRIPT = REPO_ROOT / "publish-openclaw-local-dockerfile.sh"
OPENCLAW_VERSION = "2026.10.7"


def build_env(bin_dir: Path, log_path: Path, missing_env_file: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for key in ("DOCKERHUB_NAMESPACE", "OPENCLAW_PUBLISH_IMAGE", "IMAGE_TAG", "PUSH_LATEST", "DOCKER_PLATFORM"):
        environment.pop(key, None)
    environment.update(
        ENV_FILE=str(missing_env_file),
        DOCKER_FAKE_LOG=str(log_path),
        PATH=f"{bin_dir}{os.pathsep}{environment.get('PATH', '')}",
    )
    return environment


def run_publish_script(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed repository script with Docker replaced by command capture.
        [str(PUBLISH_SCRIPT)], cwd=REPO_ROOT, env=environment, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize(
    ("configuration", "error"),
    [
        ({}, "DOCKERHUB_NAMESPACE"),
        ({"OPENCLAW_PUBLISH_IMAGE": "MyUser/OpenClaw"}, "must be lowercase"),
        ({"OPENCLAW_PUBLISH_IMAGE": "nonamespace"}, "namespace/name"),
        ({"DOCKERHUB_NAMESPACE": "acme", "IMAGE_TAG": "bad/tag"}, "not a valid Docker tag"),
    ],
)
def test_invalid_publication_is_rejected_before_docker(
    tmp_path: Path, configuration: dict[str, str], error: str
) -> None:
    # Given
    bin_dir, log_path = install_fake_docker(tmp_path)
    environment = build_env(bin_dir, log_path, tmp_path / "missing.env") | configuration

    # When
    result = run_publish_script(environment)

    # Then
    assert result.returncode == 2
    assert error in result.stderr
    assert not log_path.exists()


@pytest.mark.parametrize("push_latest", [None, "0", "true", "1"])
@pytest.mark.parametrize("local_platform", ["linux/amd64", "linux/arm64"])
def test_publisher_pushes_both_targets_in_one_build_without_local_load(
    tmp_path: Path, push_latest: str | None, local_platform: str
) -> None:
    # Given
    bin_dir, log_path = install_fake_docker(tmp_path)
    environment = build_env(bin_dir, log_path, tmp_path / "missing.env")
    environment.update(DOCKERHUB_NAMESPACE="acme", DOCKER_PLATFORM=local_platform)
    if push_latest is not None:
        environment["PUSH_LATEST"] = push_latest

    # When
    result = run_publish_script(environment)

    # Then
    assert result.returncode == 0, result.stderr
    commands = log_path.read_text().splitlines()
    assert len(commands) == 1
    command = commands[0]
    assert command.startswith("buildx build --platform linux/amd64,linux/arm64 ")
    assert f"-t acme/openclaw:{OPENCLAW_VERSION}" in command
    assert ("-t acme/openclaw:latest" in command) == (push_latest == "1")
    assert "--push" in command
    assert "--load" not in command
    assert "login" not in command
    assert f"--build-arg GOGCLI_AMD64_SHA256={GOGCLI_AMD64_SHA256}" in command
    assert f"--build-arg GOGCLI_ARM64_SHA256={GOGCLI_ARM64_SHA256}" in command
    assert "GOG_KEYRING_PASSWORD" not in command


def test_publisher_prefers_env_file_repository_tag_and_pins(tmp_path: Path) -> None:
    # Given
    bin_dir, log_path = install_fake_docker(tmp_path)
    env_file = tmp_path / "publish.env"
    env_file.write_text(
        'OPENCLAW_PUBLISH_IMAGE="file/guest"\nIMAGE_TAG="custom-tag"\n'
        'OPENCLAW_VERSION="2026.10.7"\nGOGCLI_AMD64_SHA256="custom-amd64"\n'
    )
    environment = build_env(bin_dir, log_path, env_file)
    environment.update(OPENCLAW_PUBLISH_IMAGE="process/guest", IMAGE_TAG="inherited")

    # When
    result = run_publish_script(environment)

    # Then
    assert result.returncode == 0, result.stderr
    command = log_path.read_text()
    assert "-t file/guest:custom-tag" in command
    assert "--build-arg GOGCLI_AMD64_SHA256=custom-amd64" in command
    assert "process/guest" not in command


@pytest.mark.parametrize("script", [PUBLISH_SCRIPT, REPO_ROOT / "build-openclaw-local-dockerfile.sh"])
def test_docker_failure_propagates_without_false_success(tmp_path: Path, script: Path) -> None:
    # Given
    bin_dir, log_path = install_fake_docker(tmp_path)
    environment = build_env(bin_dir, log_path, tmp_path / "missing.env")
    environment.update(DOCKERHUB_NAMESPACE="acme", DOCKER_FAKE_EXIT="41")

    # When
    result = subprocess.run(  # noqa: S603 - fixed scripts with captured Docker invocation, no registry access.
        [str(script)], cwd=REPO_ROOT, env=environment, capture_output=True, text=True, check=False
    )

    # Then
    assert result.returncode == 41
    assert len(log_path.read_text().splitlines()) == 1
    assert "Successfully" not in result.stdout
    assert "Published" not in result.stdout
