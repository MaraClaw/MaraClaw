import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLISH_SCRIPT = REPO_ROOT / "publish-openclaw-local-dockerfile.sh"
OPENCLAW_VERSION = "2026.7.1-2"


def install_fake_docker(root: Path) -> tuple[Path, Path]:
    bin_dir = root / "bin"
    bin_dir.mkdir()
    log_path = root / "docker.log"
    docker_path = bin_dir / "docker"
    docker_path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env sh",
                'printf \'%s\\n\' "$*" >> "$DOCKER_FAKE_LOG"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    docker_path.chmod(docker_path.stat().st_mode | stat.S_IXUSR)
    return bin_dir, log_path


def build_env(bin_dir: Path, log_path: Path, missing_env_file: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["ENV_FILE"] = str(missing_env_file)
    env["DOCKER_FAKE_LOG"] = str(log_path)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env.pop("DOCKERHUB_NAMESPACE", None)
    env.pop("OPENCLAW_PUBLISH_IMAGE", None)
    env.pop("IMAGE_TAG", None)
    env.pop("PUSH_LATEST", None)
    env.pop("DOCKER_PLATFORM", None)
    return env


def run_publish_script(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed repository script path under test
        [str(PUBLISH_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_publish_script_requires_dockerhub_namespace(tmp_path: Path) -> None:
    # Given
    bin_dir, log_path = install_fake_docker(tmp_path)
    env = build_env(bin_dir, log_path, tmp_path / "missing.env")

    # When
    result = run_publish_script(env)

    # Then
    assert result.returncode == 2
    assert "DOCKERHUB_NAMESPACE" in result.stderr
    assert not log_path.exists()


def test_publish_script_rejects_uppercase_repository(tmp_path: Path) -> None:
    # Given
    bin_dir, log_path = install_fake_docker(tmp_path)
    env = build_env(bin_dir, log_path, tmp_path / "missing.env")
    env["OPENCLAW_PUBLISH_IMAGE"] = "MyUser/OpenClaw"

    # When
    result = run_publish_script(env)

    # Then
    assert result.returncode == 2
    assert "must be lowercase" in result.stderr
    assert not log_path.exists()


def test_publish_script_builds_tags_and_pushes_version(tmp_path: Path) -> None:
    # Given
    bin_dir, log_path = install_fake_docker(tmp_path)
    env = build_env(bin_dir, log_path, tmp_path / "missing.env")
    env["DOCKERHUB_NAMESPACE"] = "maraclaw"

    # When
    result = run_publish_script(env)

    # Then
    log = log_path.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "-t openclaw:local" in log
    assert f"tag openclaw:local maraclaw/openclaw:{OPENCLAW_VERSION}" in log
    assert f"push maraclaw/openclaw:{OPENCLAW_VERSION}" in log
    assert "push maraclaw/openclaw:latest" not in log


def test_publish_script_uses_configured_image_and_tag(tmp_path: Path) -> None:
    # Given
    bin_dir, log_path = install_fake_docker(tmp_path)
    env = build_env(bin_dir, log_path, tmp_path / "missing.env")
    env["OPENCLAW_PUBLISH_IMAGE"] = "acme/openclaw-guest"
    env["IMAGE_TAG"] = "2026.7.1-2-tdai"

    # When
    result = run_publish_script(env)

    # Then
    log = log_path.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "tag openclaw:local acme/openclaw-guest:2026.7.1-2-tdai" in log
    assert "push acme/openclaw-guest:2026.7.1-2-tdai" in log


def test_publish_script_pushes_latest_when_requested(tmp_path: Path) -> None:
    # Given
    bin_dir, log_path = install_fake_docker(tmp_path)
    env = build_env(bin_dir, log_path, tmp_path / "missing.env")
    env["DOCKERHUB_NAMESPACE"] = "maraclaw"
    env["PUSH_LATEST"] = "1"

    # When
    result = run_publish_script(env)

    # Then
    log = log_path.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert f"push maraclaw/openclaw:{OPENCLAW_VERSION}" in log
    assert "tag openclaw:local maraclaw/openclaw:latest" in log
    assert "push maraclaw/openclaw:latest" in log


def test_publish_script_rejects_non_arm64_platform(tmp_path: Path) -> None:
    # Given
    bin_dir, log_path = install_fake_docker(tmp_path)
    env = build_env(bin_dir, log_path, tmp_path / "missing.env")
    env["DOCKERHUB_NAMESPACE"] = "maraclaw"
    env["DOCKER_PLATFORM"] = "linux/amd64"

    # When
    result = run_publish_script(env)

    # Then
    assert result.returncode == 2
    assert "gogcli is pinned to the linux/arm64 release" in result.stderr
    assert not log_path.exists()
