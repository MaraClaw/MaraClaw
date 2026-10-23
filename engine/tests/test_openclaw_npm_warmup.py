import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from test_openclaw_officecli_dockerfile import DOCKERFILE, _instructions

INSTALLER = "/usr/local/bin/bootstrap-memory-tencentdb.sh"
FAKE_INSTALLER = """#!/bin/sh
set -eu
test "$#" = 1 && test "$1" = true
test "$npm_config_offline" = false
test "$npm_config_cache" = "$WARMUP_PROBE/npm-cache"
test -f "$npm_config_cache/sentinel"
test -d "$OPENCLAW_STATE_DIR"
test -z "$(ls -A "$OPENCLAW_STATE_DIR")"
attempt=1
if [ -f "$WARMUP_PROBE/states" ]; then
    while IFS= read -r previous; do
        test ! -e "$previous"
        attempt=$((attempt + 1))
    done < "$WARMUP_PROBE/states"
fi
printf '%s\\n' "$OPENCLAW_STATE_DIR" >> "$WARMUP_PROBE/states"
printf '%s\\n' "$attempt" >> "$npm_config_cache/attempts"
mkdir "$OPENCLAW_STATE_DIR/extensions"
touch "$OPENCLAW_STATE_DIR/extensions/partial-install"
if [ "$attempt" -le "$WARMUP_FAILURES" ]; then
    echo 'npm error code ECONNRESET' >&2
    exit 17
fi
touch "$OPENCLAW_STATE_DIR/.bootstrap-tencentdb-version"
"""


@dataclass(frozen=True, slots=True)
class WarmupProbe:
    result: subprocess.CompletedProcess[str]
    states: tuple[Path, ...]
    delays: tuple[int, ...]


def _run_warmup(root: Path, failures: int) -> WarmupProbe:
    instructions, errors = _instructions(DOCKERFILE.read_text(encoding="utf-8"))
    assert errors == ()
    warmup = [body for name, body in instructions if name == "RUN"][-1]
    assert warmup.count(INSTALLER) == 1
    bin_dir = root / "bin"
    bin_dir.mkdir()
    installer = bin_dir / "installer"
    installer.write_text(FAKE_INSTALLER, encoding="utf-8")
    installer.chmod(0o755)
    sleep = bin_dir / "sleep"
    sleep.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$WARMUP_PROBE/sleeps"\n', encoding="utf-8")
    sleep.chmod(0o755)
    state_root = root / "warm-states"
    state_root.mkdir()
    cache = root / "npm-cache"
    cache.mkdir()
    (cache / "sentinel").write_text("keep npm cache", encoding="utf-8")
    (root / "states").touch()
    (root / "sleeps").touch()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(state_root),
        "WARMUP_PROBE": str(root),
        "WARMUP_FAILURES": str(failures),
        "npm_config_cache": str(cache),
        "npm_config_offline": "true",
        "OPENCLAW_STATE_DIR": str(root / "runtime-state"),
    }
    script = warmup.replace(INSTALLER, shlex.quote(str(installer)))
    script += (
        '\nstatus=$?\nprintf "%s\\n" "$npm_config_offline" "$OPENCLAW_STATE_DIR" '
        '> "$WARMUP_PROBE/after"\nexit "$status"\n'
    )
    result = subprocess.run(  # noqa: S603 - execute the repository RUN with isolated local test executables.
        ["/bin/sh", "-c", script], env=env, text=True, capture_output=True, check=False, timeout=10
    )
    return WarmupProbe(
        result=result,
        states=tuple(Path(line) for line in (root / "states").read_text(encoding="utf-8").splitlines()),
        delays=tuple(int(line) for line in (root / "sleeps").read_text(encoding="utf-8").splitlines()),
    )


def _assert_disposable_states(root: Path, probe: WarmupProbe, attempts: int) -> None:
    assert len(probe.states) == len(set(probe.states)) == attempts
    assert all(not state.exists() for state in probe.states)
    assert list((root / "warm-states").iterdir()) == []
    assert (root / "npm-cache" / "sentinel").read_text(encoding="utf-8") == "keep npm cache"
    assert (root / "npm-cache" / "attempts").read_text(encoding="utf-8").splitlines() == [
        str(attempt) for attempt in range(1, attempts + 1)
    ]


@pytest.mark.parametrize("failures", [1, 2])
def test_warmup_recovers_from_transient_econnreset_with_fresh_state(tmp_path: Path, failures: int) -> None:
    # Given an installer that fails until the next attempt.
    attempts = failures + 1

    # When the real final Docker RUN executes.
    probe = _run_warmup(tmp_path, failures)

    # Then failed and successful guest states disappear, but the npm cache persists.
    assert probe.result.returncode == 0, probe.result.stderr
    assert probe.result.stderr.count("ECONNRESET") == failures
    assert probe.delays == (5, 10)[:failures]
    _assert_disposable_states(tmp_path, probe, attempts)


def test_warmup_exhausts_three_attempts_and_preserves_failure(tmp_path: Path) -> None:
    # Given an installer that cannot succeed within the retry budget.
    failures = 99

    # When the real final Docker RUN executes.
    probe = _run_warmup(tmp_path, failures)

    # Then there is no fourth attempt or terminal sleep, and failure stays nonzero.
    assert probe.result.returncode == 17, probe.result.stderr
    assert probe.result.stderr.count("ECONNRESET") == 3
    assert probe.delays == (5, 10)
    _assert_disposable_states(tmp_path, probe, 3)


def test_warmup_first_success_stops_without_retry_or_runtime_env_leak(tmp_path: Path) -> None:
    # Given an installer that succeeds immediately.
    instructions, errors = _instructions(DOCKERFILE.read_text(encoding="utf-8"))
    assert errors == ()

    # When the real final Docker RUN executes.
    probe = _run_warmup(tmp_path, 0)

    # Then online npm is command-scoped, never baked into runtime ENV.
    assert probe.result.returncode == 0, probe.result.stderr
    assert "ECONNRESET" not in probe.result.stderr
    assert probe.delays == ()
    _assert_disposable_states(tmp_path, probe, 1)
    assert (tmp_path / "after").read_text(encoding="utf-8").splitlines() == ["true", str(tmp_path / "runtime-state")]
    assert all(
        token.split("=", 1)[0].lower() not in {"npm_config_offline", "openclaw_state_dir"}
        for name, body in instructions
        if name == "ENV"
        for token in shlex.split(body)
    )
