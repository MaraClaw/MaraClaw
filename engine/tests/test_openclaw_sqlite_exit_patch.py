import json
import shutil
import subprocess
from pathlib import Path

import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[1]
PATCH = ENGINE_ROOT / "docker/openclaw/patch-openclaw-sqlite-exit.cjs"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is required for the OpenClaw image patch tests")
# OpenClaw 2026.10.6, dist/sqlite-wal-*.mjs: original cache-exit helper.
UPSTREAM = """let lastSqliteExitGroup;
function detachEmptySqliteExitGroup(group) {
	if (group.pending.size > 0) return;
	if (lastSqliteExitGroup === group) lastSqliteExitGroup = void 0;
	process.removeListener("exit", group.dispatch);
}
/**
* Register a best-effort exit-time close for a SQLite handle cache. Returns an
* unregister callback the cache's orderly close path must invoke, so tests and
* runtime shutdowns do not accumulate listeners on shared worker processes.
*/
function registerSqliteCacheExitClose(closeAll) {
	const registration = {
		close: closeAll,
		fired: false
	};
	let group = lastSqliteExitGroup;
	if (!group || process.listeners("exit").at(-1) !== group.dispatch) {
		const pending = /* @__PURE__ */ new Set([registration]);
		const created = {
			pending,
			dispatch: () => {
				const snapshot = [...pending];
				for (const entry of snapshot) {
					if (entry.fired) continue;
					entry.fired = true;
					pending.delete(entry);
					detachEmptySqliteExitGroup(created);
					try {
						entry.close();
					} catch {}
				}
			}
		};
		process.on("exit", created.dispatch);
		lastSqliteExitGroup = group = created;
	} else group.pending.add(registration);
	const owner = group;
	return () => {
		owner.pending.delete(registration);
		detachEmptySqliteExitGroup(owner);
	};
}
"""


def _install(tmp_path: Path, source: str = UPSTREAM) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (tmp_path / "package.json").write_text(json.dumps({"name": "openclaw", "version": "2026.10.6"}))
    target = dist / "sqlite-wal-fixture.mjs"
    target.write_text(source + "export { registerSqliteCacheExitClose };\n")
    return target


def _patch(root: Path) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    return subprocess.run(  # noqa: S603 - fixed repository patch and owned temporary package.
        [NODE, str(PATCH), str(root)], capture_output=True, text=True, check=False, timeout=30
    )


def _run(target: Path, scenario: str) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    return subprocess.run(  # noqa: S603 - trusted JavaScript fixtures in an isolated Node subprocess.
        [
            NODE,
            "--trace-warnings",
            "--input-type=module",
            "-e",
            f"import {{ registerSqliteCacheExitClose as register }} from {json.dumps(target.as_uri())};\n"
            'import assert from "node:assert/strict";\n' + scenario,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_sqlite_caches_share_one_exit_listener_when_other_owners_interleave(tmp_path: Path) -> None:
    target = _install(tmp_path)
    (target.parent / "sqlite-wal-checksum-other.mjs").write_text("export const checksum = 1;\n")
    assert _patch(tmp_path).returncode == 0

    result = _run(
        target,
        """
const baseline = process.listenerCount("exit");
const releaseFirst = register(() => {});
const unrelated = () => {};
process.on("exit", unrelated);
const releases = Array.from({length: 12}, () => register(() => {}));
assert.equal(process.listenerCount("exit"), baseline + 2);
assert.equal(process.getMaxListeners(), 10);
releaseFirst();
for (const release of releases) release();
assert.equal(process.listenerCount("exit"), baseline + 1);
process.removeListener("exit", unrelated);
assert.equal(process.listenerCount("exit"), baseline);
""",
    )

    assert result.returncode == 0, result.stderr
    assert "MaxListenersExceededWarning" not in result.stderr


def test_sqlite_exit_dispatch_preserves_cancellation_and_best_effort_close(tmp_path: Path) -> None:
    target = _install(tmp_path)
    assert _patch(tmp_path).returncode == 0

    result = _run(
        target,
        """
const calls = [];
const cancel = register(() => calls.push("cancelled"));
cancel();
cancel();
register(() => { calls.push("first"); throw new Error("expected close failure"); });
process.on("exit", () => assert.deepEqual(calls, ["first", "last"]));
register(() => calls.push("last"));
""",
    )

    assert result.returncode == 0, result.stderr


def test_unrelated_listener_leaks_still_warn_after_patch(tmp_path: Path) -> None:
    target = _install(tmp_path)
    assert _patch(tmp_path).returncode == 0

    result = _run(target, 'for (let i = 0; i < 11; i++) process.on("exit", () => {});')

    assert result.returncode == 0, result.stderr
    assert "MaxListenersExceededWarning" in result.stderr


@pytest.mark.parametrize("source", [UPSTREAM.replace("pending.delete(entry)", "pending.clear()"), "// drift\n"])
def test_patch_refuses_unknown_sqlite_cleanup_source(tmp_path: Path, source: str) -> None:
    target = _install(tmp_path, source)
    before = target.read_bytes()

    result = _patch(tmp_path)

    assert result.returncode != 0
    assert target.read_bytes() == before


@pytest.mark.parametrize("version", ["2026.10.7", "2026.10.5"])
def test_patch_refuses_unreviewed_openclaw_versions(tmp_path: Path, version: str) -> None:
    target = _install(tmp_path)
    (tmp_path / "package.json").write_text(json.dumps({"name": "openclaw", "version": version}))
    before = target.read_bytes()

    result = _patch(tmp_path)

    assert result.returncode != 0
    assert target.read_bytes() == before


@pytest.mark.parametrize("candidate_count", [0, 2])
def test_patch_refuses_ambiguous_sqlite_modules(tmp_path: Path, candidate_count: int) -> None:
    target = _install(tmp_path)
    if candidate_count == 0:
        target.unlink()
    else:
        (target.parent / "sqlite-wal-another.mjs").write_text(target.read_text())

    result = _patch(tmp_path)

    assert result.returncode != 0


def test_guest_image_applies_sqlite_patch_as_root_before_runtime_user() -> None:
    instructions = (ENGINE_ROOT / "Dockerfile.openclaw").read_text()
    patch_copy = instructions.index(
        "COPY --chown=root:root docker/openclaw/patch-openclaw-sqlite-exit.cjs "
        "/usr/local/bin/patch-openclaw-sqlite-exit.cjs"
    )
    patch_run = instructions.index(
        'RUN node /usr/local/bin/patch-openclaw-sqlite-exit.cjs "$(npm root --global)/openclaw"'
    )

    assert instructions.index("USER root") < patch_copy < patch_run < instructions.rindex("USER node")
