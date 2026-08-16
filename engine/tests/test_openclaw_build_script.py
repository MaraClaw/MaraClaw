import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = REPO_ROOT / "build-openclaw-local-dockerfile.sh"
DOCKERFILE = REPO_ROOT / "Dockerfile.openclaw"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
OPENCLAW_VERSION = "2026.7.1-2"
OPENCLAW_SHA256 = "sha256:5bb525f36f471a41239615d321c441778c7e1c007018ed6d84b795be77803276"
GOGCLI_VERSION = "0.37.0"
GOGCLI_SHA256 = "sha256:4abde90c4e74ceb125f3fdd87676ff7958e89f7820978ffa11de26aee06e721c"


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
    return env


def run_build_script(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed repository script path under test
        [str(BUILD_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_openclaw_build_script_uses_arm64_platform_by_default(tmp_path: Path) -> None:
    # Given
    bin_dir, log_path = install_fake_docker(tmp_path)
    env = build_env(bin_dir, log_path, tmp_path / "missing.env")

    # When
    result = run_build_script(env)

    # Then
    assert result.returncode == 0, result.stderr
    assert "--platform linux/arm64" in log_path.read_text(encoding="utf-8")


def test_openclaw_build_script_passes_default_openclaw_version_and_sha(tmp_path: Path) -> None:
    # Given
    bin_dir, log_path = install_fake_docker(tmp_path)
    env = build_env(bin_dir, log_path, tmp_path / "missing.env")

    # When
    result = run_build_script(env)

    # Then
    command = log_path.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert f"--build-arg OPENCLAW_VERSION={OPENCLAW_VERSION}" in command
    assert f"--build-arg OPENCLAW_SHA256={OPENCLAW_SHA256}" in command


def test_openclaw_build_script_passes_default_gogcli_version_and_sha(tmp_path: Path) -> None:
    # Given
    bin_dir, log_path = install_fake_docker(tmp_path)
    env = build_env(bin_dir, log_path, tmp_path / "missing.env")

    # When
    result = run_build_script(env)

    # Then
    command = log_path.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert f"--build-arg GOGCLI_VERSION={GOGCLI_VERSION}" in command
    assert f"--build-arg GOGCLI_SHA256={GOGCLI_SHA256}" in command


def test_environment_example_pins_openclaw_and_gogcli_releases() -> None:
    # Given
    expected_pins = {
        "OPENCLAW_VERSION": OPENCLAW_VERSION,
        "OPENCLAW_SHA256": OPENCLAW_SHA256,
        "GOGCLI_VERSION": GOGCLI_VERSION,
        "GOGCLI_SHA256": GOGCLI_SHA256,
    }
    content = ENV_EXAMPLE.read_text(encoding="utf-8")

    # When
    example_pins = {
        key: value
        for line in content.splitlines()
        for key, separator, value in (line.partition("="),)
        if separator and key in expected_pins
    }

    # Then
    assert example_pins == expected_pins


def test_openclaw_build_script_passes_configured_openclaw_version_and_sha(tmp_path: Path) -> None:
    # Given
    bin_dir, log_path = install_fake_docker(tmp_path)
    env = build_env(bin_dir, log_path, tmp_path / "missing.env")
    env["OPENCLAW_VERSION"] = "2026.6.12"
    env["OPENCLAW_SHA256"] = "sha256:configured"

    # When
    result = run_build_script(env)

    # Then
    command = log_path.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "--build-arg OPENCLAW_VERSION=2026.6.12" in command
    assert "--build-arg OPENCLAW_SHA256=sha256:configured" in command


def test_openclaw_build_script_passes_configured_gogcli_version_and_sha(tmp_path: Path) -> None:
    # Given
    bin_dir, log_path = install_fake_docker(tmp_path)
    env = build_env(bin_dir, log_path, tmp_path / "missing.env")
    env["GOGCLI_VERSION"] = "0.99.0"
    env["GOGCLI_SHA256"] = "configured-gogcli-sha"

    # When
    result = run_build_script(env)

    # Then
    command = log_path.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "--build-arg GOGCLI_VERSION=0.99.0" in command
    assert "--build-arg GOGCLI_SHA256=configured-gogcli-sha" in command


def test_openclaw_build_script_rejects_non_arm64_platform(tmp_path: Path) -> None:
    # Given
    bin_dir, log_path = install_fake_docker(tmp_path)
    env = build_env(bin_dir, log_path, tmp_path / "missing.env")
    env["DOCKER_PLATFORM"] = "linux/amd64"

    # When
    result = run_build_script(env)

    # Then
    assert result.returncode == 2
    assert "gogcli is pinned to the linux/arm64 release" in result.stderr
    assert not log_path.exists()


def test_dockerfile_pins_node_26_7_0_base_image() -> None:
    # Given
    content = DOCKERFILE.read_text(encoding="utf-8")

    # When
    base_image_lines = [
        line.strip() for line in content.splitlines() if line.strip().startswith("ARG OPENCLAW_BASE_IMAGE=")
    ]

    # Then
    assert base_image_lines == ["ARG OPENCLAW_BASE_IMAGE=node:26.7.0-bookworm-slim"]


def test_openclaw_dockerfile_pins_bookworm_packages_without_hadolint_ignore() -> None:
    # Given
    content = DOCKERFILE.read_text(encoding="utf-8")

    # When
    package_install_block = "\n".join(
        line.strip()
        for line in content.splitlines()
        if "ca-certificates" in line or "curl" in line or "hadolint ignore=DL3008" in line
    )

    # Then
    assert "CA_CERTIFICATES_VERSION=20230311+deb12u1" in content
    assert "CURL_VERSION=7.88.1-10+deb12u15" in content
    assert '"ca-certificates=${CA_CERTIFICATES_VERSION}"' in package_install_block
    assert '"curl=${CURL_VERSION}"' in package_install_block
    assert "hadolint ignore=DL3008" not in package_install_block


def test_openclaw_dockerfile_verifies_openclaw_npm_tarball_before_install() -> None:
    # Given
    content = DOCKERFILE.read_text(encoding="utf-8")

    # When
    checksum_block = "\n".join(
        line.strip()
        for line in content.splitlines()
        if "openclaw" in line.lower() or "OPENCLAW_SHA256" in line or "OPENCLAW_TARBALL" in line
    )

    # Then
    assert f"OPENCLAW_VERSION={OPENCLAW_VERSION}" in content
    assert f"OPENCLAW_SHA256={OPENCLAW_SHA256}" in content
    assert 'npm pack "openclaw@${OPENCLAW_VERSION}"' in checksum_block
    assert 'printf \'%s  %s\\n\' "${OPENCLAW_SHA256#sha256:}" "$OPENCLAW_TARBALL"' in checksum_block
    assert "sha256sum -c /tmp/openclaw.tgz.sha256" in checksum_block
    assert 'npm install -g "$OPENCLAW_TARBALL"' in checksum_block
    assert 'npm install -g "openclaw@${OPENCLAW_VERSION}"' not in content


def test_openclaw_dockerfile_pins_and_verifies_gogcli_release_archive() -> None:
    # Given
    content = DOCKERFILE.read_text(encoding="utf-8")

    # When
    gogcli_block = "\n".join(
        line.strip()
        for line in content.splitlines()
        if "GOGCLI" in line or "gogcli" in line or "/usr/local/bin/gog" in line
    )

    # Then
    assert f"GOGCLI_VERSION={GOGCLI_VERSION}" in content
    assert f"GOGCLI_SHA256={GOGCLI_SHA256}" in content
    assert (
        '"https://github.com/openclaw/gogcli/releases/download/v${GOGCLI_VERSION}/gogcli_${GOGCLI_VERSION}_linux_arm64.tar.gz"'
        in gogcli_block
    )
    assert "printf '%s  %s\\n' \"${GOGCLI_SHA256#sha256:}\" /tmp/gogcli.tar.gz" in gogcli_block
    assert "sha256sum -c /tmp/gogcli.tar.gz.sha256" in gogcli_block
    assert "tar -xzf /tmp/gogcli.tar.gz -C /usr/local/bin ./gog" in gogcli_block
