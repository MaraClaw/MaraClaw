import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "docker" / "openclaw" / "bootstrap-memory-tencentdb.sh"
MARKER_FILENAME = ".bootstrap-tencentdb-version"
DEFAULT_VERSION = "1"
PACKAGE_NAME = "@tencentdb-agent-memory/memory-tencentdb"


@dataclass(frozen=True, slots=True)
class BootstrapPaths:
    state_dir: Path
    bin_dir: Path
    log_path: Path
    checksum_log_path: Path
    archive_path: Path


def install_fake_tools(root: Path) -> tuple[Path, Path, Path]:
    bin_dir = root / "bin"
    bin_dir.mkdir()
    log_path = root / "openclaw.log"
    checksum_log_path = root / "sha256sum.log"
    openclaw_path = bin_dir / "openclaw"
    openclaw_path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import os",
                "import sys",
                "from pathlib import Path",
                "",
                'log_path = Path(os.environ["OPENCLAW_FAKE_LOG"])',
                'with log_path.open("a", encoding="utf-8") as log_file:',
                '    log_file.write("\\0".join(sys.argv[1:]) + "\\n")',
                "",
                'if os.environ.get("OPENCLAW_FAKE_FAIL") == "1":',
                "    raise SystemExit(17)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    openclaw_path.chmod(openclaw_path.stat().st_mode | stat.S_IXUSR)
    checksum_path = bin_dir / "sha256sum"
    checksum_path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import os",
                "import sys",
                "from pathlib import Path",
                "",
                'log_path = Path(os.environ["SHA256_FAKE_LOG"])',
                'with log_path.open("a", encoding="utf-8") as log_file:',
                '    log_file.write("\\0".join(sys.argv[1:]) + "\\n")',
                "",
                'if os.environ.get("SHA256_FAKE_FAIL") == "1":',
                "    raise SystemExit(18)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    checksum_path.chmod(checksum_path.stat().st_mode | stat.S_IXUSR)
    return bin_dir, log_path, checksum_log_path


def create_bootstrap_paths(root: Path, archive_present: bool = True) -> BootstrapPaths:
    state_dir = root / "state"
    cache_dir = root / "cache"
    state_dir.mkdir()
    cache_dir.mkdir()
    archive_path = cache_dir / "memory-tencentdb.tgz"
    if archive_present:
        archive_path.write_text("package archive", encoding="utf-8")
        (cache_dir / "memory-tencentdb.tgz.sha256").write_text("checksum  memory-tencentdb.tgz", encoding="utf-8")
    bin_dir, log_path, checksum_log_path = install_fake_tools(root)
    return BootstrapPaths(
        state_dir=state_dir,
        bin_dir=bin_dir,
        log_path=log_path,
        checksum_log_path=checksum_log_path,
        archive_path=archive_path,
    )


def build_env(paths: BootstrapPaths, version: str = DEFAULT_VERSION) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "OPENCLAW_MEMORY_TENCENTDB_ARCHIVE": str(paths.archive_path),
            "TENCENTDB_PLUGIN_VERSION": version,
            "OPENCLAW_STATE_DIR": str(paths.state_dir),
            "PATH": f"{paths.bin_dir}{os.pathsep}{env['PATH']}",
            "OPENCLAW_FAKE_LOG": str(paths.log_path),
            "SHA256_FAKE_LOG": str(paths.checksum_log_path),
        }
    )
    return env


def run_bootstrap(env: dict[str, str], *command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - test harness executes fixed local script with controlled args.
        ["/bin/sh", str(SCRIPT_PATH), *command],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def openclaw_calls(log_path: Path) -> list[list[str]]:
    if not log_path.exists():
        return []
    return [line.split("\0") for line in log_path.read_text(encoding="utf-8").splitlines()]


def checksum_calls(log_path: Path) -> list[list[str]]:
    if not log_path.exists():
        return []
    return [line.split("\0") for line in log_path.read_text(encoding="utf-8").splitlines()]


def install_poison_patch_asset(paths: BootstrapPaths) -> None:
    script_path = paths.archive_path.parent / "openclaw-after-tool-call-messages.patch.sh"
    script_path.write_text(
        "#!/usr/bin/env sh\nexit 19\n",
        encoding="utf-8",
    )
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)


def test_first_run_installs_archive_and_writes_version_marker(tmp_path: Path) -> None:
    # Given
    paths = create_bootstrap_paths(tmp_path)
    install_poison_patch_asset(paths)
    env = build_env(paths)

    # When
    result = run_bootstrap(env, "sh", "-c", "printf openclaw-ok")

    # Then
    assert result.returncode == 0, result.stderr
    assert result.stdout == "openclaw-ok"
    assert checksum_calls(paths.checksum_log_path) == [["-c", f"{paths.archive_path}.sha256"]]
    assert openclaw_calls(paths.log_path) == [["plugins", "install", str(paths.archive_path), "--pin"]]
    assert (paths.state_dir / MARKER_FILENAME).read_text(encoding="utf-8").strip() == DEFAULT_VERSION


def test_second_run_skips_install_when_marker_matches_version(tmp_path: Path) -> None:
    # Given
    paths = create_bootstrap_paths(tmp_path)
    (paths.state_dir / MARKER_FILENAME).write_text(DEFAULT_VERSION, encoding="utf-8")
    env = build_env(paths)

    # When
    result = run_bootstrap(env, "sh", "-c", "printf openclaw-ok")

    # Then
    assert result.returncode == 0, result.stderr
    assert result.stdout == "openclaw-ok"
    assert checksum_calls(paths.checksum_log_path) == []
    assert openclaw_calls(paths.log_path) == []
    assert (paths.state_dir / MARKER_FILENAME).read_text(encoding="utf-8") == DEFAULT_VERSION


def test_version_bump_triggers_reinstall_and_marker_update(tmp_path: Path) -> None:
    # Given
    paths = create_bootstrap_paths(tmp_path)
    (paths.state_dir / MARKER_FILENAME).write_text(DEFAULT_VERSION, encoding="utf-8")
    install_poison_patch_asset(paths)
    env = build_env(paths, version="2")

    # When
    result = run_bootstrap(env, "sh", "-c", "printf openclaw-ok")

    # Then
    assert result.returncode == 0, result.stderr
    assert result.stdout == "openclaw-ok"
    assert checksum_calls(paths.checksum_log_path) == [["-c", f"{paths.archive_path}.sha256"]]
    assert openclaw_calls(paths.log_path) == [["plugins", "install", str(paths.archive_path), "--pin"]]
    assert (paths.state_dir / MARKER_FILENAME).read_text(encoding="utf-8").strip() == "2"


def test_disabled_memory_skips_install_and_still_execs_command(tmp_path: Path) -> None:
    # Given
    paths = create_bootstrap_paths(tmp_path)
    env = build_env(paths)
    env["OPENCLAW_MEMORY_TENCENTDB_ENABLED"] = "false"

    # When
    result = run_bootstrap(env, "sh", "-c", "printf openclaw-ok")

    # Then
    assert result.returncode == 0, result.stderr
    assert result.stdout == "openclaw-ok"
    assert checksum_calls(paths.checksum_log_path) == []
    assert openclaw_calls(paths.log_path) == []
    assert not (paths.state_dir / MARKER_FILENAME).exists()


def test_no_arguments_default_to_openclaw_gateway(tmp_path: Path) -> None:
    # Given
    paths = create_bootstrap_paths(tmp_path)
    (paths.state_dir / MARKER_FILENAME).write_text(DEFAULT_VERSION, encoding="utf-8")
    env = build_env(paths)

    # When
    result = run_bootstrap(env)

    # Then
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert checksum_calls(paths.checksum_log_path) == []
    assert openclaw_calls(paths.log_path) == [["gateway"]]


def test_install_spec_env_does_not_override_baked_archive(tmp_path: Path) -> None:
    # Given
    paths = create_bootstrap_paths(tmp_path)
    env = build_env(paths)
    env["OPENCLAW_MEMORY_TENCENTDB_INSTALL_SPEC"] = "malicious-package@latest"

    # When
    result = run_bootstrap(env, "sh", "-c", "printf openclaw-ok")

    # Then
    assert result.returncode == 0, result.stderr
    assert checksum_calls(paths.checksum_log_path) == [["-c", f"{paths.archive_path}.sha256"]]
    assert openclaw_calls(paths.log_path) == [["plugins", "install", str(paths.archive_path), "--pin"]]


def test_failed_checksum_exits_nonzero_and_does_not_install_or_write_marker(tmp_path: Path) -> None:
    # Given
    paths = create_bootstrap_paths(tmp_path)
    env = build_env(paths)
    env["SHA256_FAKE_FAIL"] = "1"

    # When
    result = run_bootstrap(env, "sh", "-c", "printf openclaw-ok")

    # Then
    assert result.returncode == 18
    assert result.stdout == ""
    assert checksum_calls(paths.checksum_log_path) == [["-c", f"{paths.archive_path}.sha256"]]
    assert openclaw_calls(paths.log_path) == []
    assert not (paths.state_dir / MARKER_FILENAME).exists()


def test_failed_install_exits_nonzero_and_does_not_write_marker(tmp_path: Path) -> None:
    # Given
    paths = create_bootstrap_paths(tmp_path)
    env = build_env(paths)
    env["OPENCLAW_FAKE_FAIL"] = "1"

    # When
    result = run_bootstrap(env, "sh", "-c", "printf openclaw-ok")

    # Then
    assert result.returncode == 17
    assert result.stdout == ""
    assert checksum_calls(paths.checksum_log_path) == [["-c", f"{paths.archive_path}.sha256"]]
    assert not (paths.state_dir / MARKER_FILENAME).exists()


def test_missing_archive_installs_from_pinned_npm_spec_and_command_still_execs(tmp_path: Path) -> None:
    # Given
    paths = create_bootstrap_paths(tmp_path, archive_present=False)
    env = build_env(paths)

    # When
    result = run_bootstrap(env, "sh", "-c", "printf openclaw-ok")

    # Then
    assert result.returncode == 0, result.stderr
    assert result.stdout == "openclaw-ok"
    assert checksum_calls(paths.checksum_log_path) == []
    assert openclaw_calls(paths.log_path) == [["plugins", "install", f"{PACKAGE_NAME}@{DEFAULT_VERSION}", "--pin"]]
