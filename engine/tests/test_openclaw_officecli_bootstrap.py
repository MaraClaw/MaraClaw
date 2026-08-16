from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
WRAPPER_SOURCE: Final = REPO_ROOT / "docker" / "openclaw" / "bootstrap-officecli.sh"
TENCENT_SOURCE: Final = REPO_ROOT / "docker" / "openclaw" / "bootstrap-memory-tencentdb.sh"
OFFICECLI_TAG: Final = "v1.0.144"
OFFICECLI_ARM64_SHA256: Final = "56ec2c3114b66f6490888b6778cbb8413a65911a26cacc7207f29e13424966da"
OFFICECLI_SKILL_SHA256: Final = "c950d285ce60021712b4753fb2d9f592308d5622bab776229061dfecb1ce55d4"
RELEASE_ROOT: Final = "https://github.com/iOfficeAI/OfficeCLI/releases/download"
SKILL_ROOT: Final = "https://raw.githubusercontent.com/iOfficeAI/OfficeCLI"
FAKE_GNU_MV: Final = """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

arguments = sys.argv[1:]
if log_path := os.environ.get("FAKE_MV_LOG"): Path(log_path).open("a", encoding="utf-8").write("\\0".join(arguments) + "\\n")
if arguments[:2] in (["-T", "--"], ["-Tf", "--"]): arguments = arguments[2:]
source, destination = map(Path, arguments)
mode = os.environ.get("OFFICECLI_FAKE_MODE")
if mode == "publish_fail" and source.name.startswith(".staging."): raise SystemExit(85)
if mode == "activation_fail" and source.name.startswith(".current."): raise SystemExit(86)
os.replace(source, destination)
"""


@dataclass(frozen=True, slots=True)
class BootstrapFixture:
    root: Path
    wrapper: Path
    state_dir: Path
    fixture_dir: Path
    bin_dir: Path
    curl_log: Path
    tencent_sentinel: Path
    child_sentinel: Path
    poison_path: Path

    def configure_release(self, tag: str, label: str) -> None:
        binary = f"#!/usr/bin/env sh\nprintf '%s\\n' 'officecli {label}'\n".encode()
        skill = f"---\nname: officecli\ndescription: {label}\n---\n".encode()
        digest = hashlib.sha256(binary).hexdigest()
        skill_digest = hashlib.sha256(skill).hexdigest()
        (self.fixture_dir / "officecli-linux-arm64").write_bytes(binary)
        (self.fixture_dir / "SHA256SUMS").write_text(f"{digest}  officecli-linux-arm64\n", encoding="utf-8")
        (self.fixture_dir / "SKILL.md").write_bytes(skill)
        wrapper_source = WRAPPER_SOURCE.read_text(encoding="utf-8")
        fixture_wrapper = (
            wrapper_source.replace(f'OFFICECLI_TAG="{OFFICECLI_TAG}"', f'OFFICECLI_TAG="{tag}"')
            .replace(f'OFFICECLI_ARM64_SHA256="{OFFICECLI_ARM64_SHA256}"', f'OFFICECLI_ARM64_SHA256="{digest}"')
            .replace(f'OFFICECLI_SKILL_SHA256="{OFFICECLI_SKILL_SHA256}"', f'OFFICECLI_SKILL_SHA256="{skill_digest}"')
        )
        assert fixture_wrapper != wrapper_source
        self.wrapper.write_text(fixture_wrapper, encoding="utf-8")

    def environment(self, mode: str = "") -> Mapping[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "OPENCLAW_STATE_DIR": str(self.state_dir),
                "OPENCLAW_MEMORY_TENCENTDB_ENABLED": "true",
                "OPENCLAW_MEMORY_TENCENTDB_ARCHIVE": str(self.root / "missing-memory-tencentdb.tgz"),
                "PATH": os.pathsep.join((str(self.bin_dir), str(self.root / "inherited-bin"), environment["PATH"])),
                "FAKE_CURL_FIXTURES": str(self.fixture_dir),
                "FAKE_CURL_LOG": str(self.curl_log),
                "OFFICECLI_FAKE_MODE": mode,
                "TENCENT_SENTINEL": str(self.tencent_sentinel),
                "CHILD_SENTINEL": str(self.child_sentinel),
                "OFFICECLI_POISON_PATH": str(self.poison_path),
                "OFFICECLI_SKIP_UPDATE": "1",
                "OFFICECLI_NO_AUTO_RESIDENT": "1",
                "HTTP_PROXY": "http://127.0.0.1:9",
                "HTTPS_PROXY": "http://127.0.0.1:9",
                "http_proxy": "http://127.0.0.1:9",
                "https_proxy": "http://127.0.0.1:9",
                "NO_PROXY": "",
                "no_proxy": "",
            }
        )
        return environment

    def run(self, *argv: str, mode: str = "") -> subprocess.CompletedProcess[str]:
        if mode in {"publish_fail", "activation_fail"} and not self.bin_dir.joinpath("mv").exists():
            _write_executable(self.bin_dir / "mv", FAKE_GNU_MV)
        return subprocess.run(  # noqa: S603 - controlled local wrapper and fake-network fixture.
            ["/bin/sh", str(self.wrapper), *argv],
            cwd=self.root,
            env=self.environment(mode),
            text=True,
            capture_output=True,
            check=False,
        )

    def requested_urls(self) -> list[str]:
        if not self.curl_log.exists():
            return []
        return [line.split("\0")[-1] for line in self.curl_log.read_text(encoding="utf-8").splitlines()]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def create_fake_network_fixture(root: Path) -> BootstrapFixture:
    script_dir = root / "script-copy"
    script_dir.mkdir()
    wrapper = script_dir / "bootstrap-officecli.sh"
    shutil.copy2(WRAPPER_SOURCE, wrapper)
    shutil.copy2(TENCENT_SOURCE, script_dir / "bootstrap-memory-tencentdb.sh")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    (script_dir / "bootstrap-memory-tencentdb.sh").chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

    fixture_dir = root / "release-fixtures"
    fixture_dir.mkdir()
    bin_dir = root / "fake-bin"
    bin_dir.mkdir()
    inherited_bin = root / "inherited-bin"
    inherited_bin.mkdir()
    _write_executable(inherited_bin / "officecli", "#!/usr/bin/env sh\nexit 99\n")
    _write_executable(
        bin_dir / "openclaw",
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

arguments = sys.argv[1:]
if arguments[:2] == ["plugins", "install"]:
    Path(os.environ["TENCENT_SENTINEL"]).write_text("entered", encoding="utf-8")
else:
    Path(os.environ["CHILD_SENTINEL"]).write_text("\\0".join(arguments), encoding="utf-8")
""",
    )
    _write_executable(
        bin_dir / "curl",
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

arguments = sys.argv[1:]
with Path(os.environ["FAKE_CURL_LOG"]).open("a", encoding="utf-8") as log_file:
    log_file.write("\\0".join(arguments) + "\\n")
url = arguments[-1]
output_path = arguments[arguments.index("-o") + 1]
mode = os.environ["OFFICECLI_FAKE_MODE"]
fixtures = Path(os.environ["FAKE_CURL_FIXTURES"])
if url.endswith("/officecli-linux-arm64"):
    if mode == "binary_fail":
        raise SystemExit(23)
    source = fixtures / "officecli-linux-arm64"
elif url.endswith("/SHA256SUMS"):
    if mode == "manifest_download_fail":
        raise SystemExit(26)
    source = fixtures / "SHA256SUMS"
    if mode == "manifest_bad":
        Path(output_path).write_text(
            f'$(touch {os.environ["OFFICECLI_POISON_PATH"]})  officecli-linux-arm64\\n', encoding="utf-8"
        )
        raise SystemExit(0)
elif url.endswith("/SKILL.md"):
    if mode == "skill_fail":
        raise SystemExit(24)
    source = fixtures / "SKILL.md"
else:
    raise SystemExit(25)
Path(output_path).write_bytes(source.read_bytes())
""",
    )
    if sys.platform == "darwin":
        _write_executable(bin_dir / "mv", FAKE_GNU_MV)
    return BootstrapFixture(
        root=root,
        wrapper=wrapper,
        state_dir=root / "state with spaces",
        fixture_dir=fixture_dir,
        bin_dir=bin_dir,
        curl_log=root / "curl.log",
        tencent_sentinel=root / "tencent-entered",
        child_sentinel=root / "child-entered",
        poison_path=root / "poisoned",
    )


def expected_urls(tag: str) -> list[str]:
    return [
        f"{RELEASE_ROOT}/{tag}/officecli-linux-arm64",
        f"{RELEASE_ROOT}/{tag}/SHA256SUMS",
        f"{SKILL_ROOT}/{tag}/SKILL.md",
    ]


def test_runtime_bootstrap_pins_officecli_to_committed_tag_and_digest() -> None:
    # Given
    source = WRAPPER_SOURCE.read_text(encoding="utf-8")

    # When
    has_committed_tag = f'OFFICECLI_TAG="{OFFICECLI_TAG}"' in source
    has_committed_digest = f'OFFICECLI_ARM64_SHA256="{OFFICECLI_ARM64_SHA256}"' in source
    has_committed_skill = f'OFFICECLI_SKILL_SHA256="{OFFICECLI_SKILL_SHA256}"' in source

    # Then
    assert has_committed_tag
    assert has_committed_digest
    assert has_committed_skill
    assert "readonly OFFICECLI_TAG OFFICECLI_ARM64_SHA256 OFFICECLI_SKILL_SHA256" in source
    assert "releases/latest" not in source
    assert f"{RELEASE_ROOT}/$OFFICECLI_TAG" in source
    assert f"{SKILL_ROOT}/$OFFICECLI_TAG/SKILL.md" in source


def test_installs_complete_pair_before_downstream_with_runtime_flags(tmp_path: Path) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    fixture.configure_release("v1.2.3", "first")
    child_command = (
        'printf "%s\\n%s\\n%s\\n" "$(command -v officecli)" "$OFFICECLI_SKIP_UPDATE" '
        '"$OFFICECLI_NO_AUTO_RESIDENT" > "$CHILD_SENTINEL"'
    )

    # When
    result = fixture.run("sh", "-c", child_command)

    # Then
    release = fixture.state_dir / ".officecli" / "releases" / "v1.2.3"
    assert result.returncode == 0, result.stderr
    assert fixture.tencent_sentinel.read_text(encoding="utf-8") == "entered"
    assert fixture.child_sentinel.read_text(encoding="utf-8").splitlines() == [
        str(fixture.state_dir / ".officecli" / "current" / "officecli"),
        "1",
        "1",
    ]
    assert release.joinpath("officecli").is_file()
    assert not release.joinpath("officecli").is_symlink()
    assert os.access(release / "officecli", os.X_OK)
    assert release.joinpath("skill", "SKILL.md").is_file()
    assert not release.joinpath("skill", "SKILL.md").is_symlink()
    assert release.joinpath("skill", "SKILL.md").stat().st_size > 0
    assert "tag=v1.2.3" in release.joinpath("metadata").read_text(encoding="utf-8")
    assert os.readlink(fixture.state_dir / ".officecli" / "current") == "releases/v1.2.3"
    assert os.readlink(fixture.state_dir / "skills" / "officecli") == "../.officecli/current/skill"
    assert fixture.requested_urls() == expected_urls("v1.2.3")


def test_refreshes_same_tag_without_skipping_then_selects_newer_tag_on_every_start(tmp_path: Path) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    fixture.configure_release("v1.0.0", "first")
    assert fixture.run("sh", "-c", ":").returncode == 0
    fixture.configure_release("v1.0.0", "first")

    # When
    same_tag_result = fixture.run("sh", "-c", ":")
    fixture.configure_release("v1.0.1", "newer-release")
    newer_tag_result = fixture.run("sh", "-c", ":")

    # Then
    root = fixture.state_dir / ".officecli"
    assert same_tag_result.returncode == 0, same_tag_result.stderr
    assert newer_tag_result.returncode == 0, newer_tag_result.stderr
    assert "first" in root.joinpath("releases", "v1.0.0", "officecli").read_text(encoding="utf-8")
    assert "newer-release" in root.joinpath("releases", "v1.0.1", "officecli").read_text(encoding="utf-8")
    assert os.readlink(root / "current") == "releases/v1.0.1"
    assert fixture.requested_urls() == expected_urls("v1.0.0") * 2 + expected_urls("v1.0.1")


def test_no_arguments_reaches_unchanged_tencent_gateway_default(tmp_path: Path) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    fixture.configure_release("v2.0.0", "gateway")

    # When
    result = fixture.run()

    # Then
    assert result.returncode == 0, result.stderr
    assert fixture.tencent_sentinel.read_text(encoding="utf-8") == "entered"
    assert fixture.child_sentinel.read_text(encoding="utf-8") == "gateway"
    assert fixture.requested_urls() == expected_urls("v2.0.0")


@pytest.mark.parametrize("mode", ["binary_fail", "manifest_bad", "skill_fail"])
def test_initial_fetch_failures_block_tencent_and_child_sentinels(tmp_path: Path, mode: str) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    fixture.configure_release("v3.0.0", "failure")

    # When
    result = fixture.run("sh", "-c", 'printf reached > "$CHILD_SENTINEL"', mode=mode)

    # Then
    assert result.returncode != 0
    assert not fixture.tencent_sentinel.exists()
    assert not fixture.child_sentinel.exists()
    assert not fixture.poison_path.exists()
