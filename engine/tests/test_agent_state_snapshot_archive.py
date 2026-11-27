"""The checks a fresh backup must pass before it is published as the agent's snapshot."""

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from app.services.agent_state_snapshot import AGENT_DB, ARCHIVE_NAME
from app.services.agent_state_snapshot_archive import SnapshotRejectedError, check_backup_report, inspect_archive
from state_snapshot_fakes import (
    STATE_ASSET,
    agent_database,
    backup_report,
    openclaw_state,
    sqlite_file,
    write_archive,
    write_file,
)

LIMIT = 1024**3


def _inspect(tmp_path: Path, archive: Path, max_bytes: int = LIMIT) -> None:
    scratch = tmp_path / "inspect"
    scratch.mkdir(parents=True)
    inspect_archive(archive, scratch, max_bytes)


def _sessions(state: Path, entries: Sequence[Mapping[str, object]], *, pending: int = 0, inputs: bool = True) -> None:
    (state / AGENT_DB).unlink()
    _ = agent_database(state / AGENT_DB, entries=entries, pending=pending, inputs_table=inputs)


def test_a_verified_backup_of_the_state_directory_alone_is_accepted() -> None:
    check_backup_report("Backing up /home/node/custom-state\n" + backup_report())


@pytest.mark.parametrize(
    "report",
    [
        pytest.param("Backup failed", id="no report"),
        pytest.param("{not json", id="not json"),
        pytest.param("[]", id="not an object"),
        pytest.param(backup_report(verified=False), id="unverified"),
        pytest.param(backup_report(includeWorkspace=True), id="workspace included"),
        pytest.param(backup_report(assets=[STATE_ASSET, {**STATE_ASSET, "kind": "workspace"}]), id="extra asset"),
        pytest.param(backup_report(assets=[]), id="no asset"),
        pytest.param(backup_report(assets=[{**STATE_ASSET, "kind": "config"}]), id="config asset"),
        pytest.param(backup_report(assets=[{**STATE_ASSET, "sourcePath": "/home/node"}]), id="other source"),
        pytest.param(
            backup_report(assets=[{**STATE_ASSET, "archivePath": "root/payload/posix/home"}]), id="other path"
        ),
    ],
)
def test_any_other_report_discards_the_backup(report: str) -> None:
    with pytest.raises(SnapshotRejectedError):
        check_backup_report(report)


@pytest.mark.parametrize(
    ("entries", "inputs"),
    [
        pytest.param([{"status": "done"}], True, id="finished sessions"),
        pytest.param([{"status": "done", "lifecycleRunId": None, "resumeAfterRestart": False}], True, id="falsy flags"),
        pytest.param([{"status": "done"}], False, id="no pending input table"),
    ],
)
def test_an_idle_archive_with_finished_journals_and_intact_databases_passes(
    tmp_path: Path, entries: list[dict[str, object]], inputs: bool
) -> None:
    # Given an idle agent whose sidecars are empty, as SQLite leaves them after a checkpoint.
    state = openclaw_state(tmp_path / "state")
    _sessions(state, entries, inputs=inputs)
    _ = write_file(state / "state" / "openclaw.sqlite-wal", b"")
    _ = write_file(state / "knowledge" / "registry.sqlite-journal", b"")
    # Then its archive passes every check.
    _inspect(tmp_path, write_archive(tmp_path / "out" / ARCHIVE_NAME, state))


def _corrupt_page(state: Path) -> None:
    database = sqlite_file(state / "knowledge" / "registry.sqlite", *(["claim" * 400] * 40))
    data = bytearray(database.read_bytes())
    data[4096 + 8 : 4096 + 400] = b"\xa5" * 392
    _ = database.write_bytes(bytes(data))


@pytest.mark.parametrize(
    "spoil",
    [
        pytest.param(lambda state: write_file(state / "state" / "openclaw.sqlite-wal", b"frame"), id="unfinished wal"),
        pytest.param(
            lambda state: write_file(state / "knowledge" / "registry.sqlite-journal", b"page"), id="unfinished journal"
        ),
        pytest.param(
            lambda state: write_file(state / "client" / "credentials.sqlite", b"not a database" * 100),
            id="garbled database",
        ),
        pytest.param(_corrupt_page, id="corrupt page"),
        pytest.param(
            lambda state: _sessions(state, [{"status": "done"}, {"lifecycleRunId": "run-1"}]), id="run in flight"
        ),
        pytest.param(
            lambda state: _sessions(state, [{"status": "done", "resumeAfterRestart": True}]), id="resume after restart"
        ),
        pytest.param(lambda state: _sessions(state, [{"mainRestartRecovery": {"turn": 3}}]), id="restart recovery"),
        pytest.param(lambda state: _sessions(state, [{"status": "done"}], pending=1), id="pending input"),
    ],
)
def test_a_backup_mid_run_or_with_a_damaged_database_is_rejected(
    tmp_path: Path, spoil: Callable[[Path], object]
) -> None:
    # Given an archive of a state directory that is mid-run, holds an unfinished journal, or a damaged database.
    state = openclaw_state(tmp_path / "state")
    _ = spoil(state)
    archive = write_archive(tmp_path / "out" / ARCHIVE_NAME, state)
    # Then it is rejected.
    with pytest.raises(SnapshotRejectedError):
        _inspect(tmp_path, archive)


def test_an_archive_over_the_size_limit_is_rejected(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "out" / ARCHIVE_NAME, openclaw_state(tmp_path / "state"))
    _inspect(tmp_path, archive, max_bytes=archive.stat().st_size)
    with pytest.raises(SnapshotRejectedError, match="limit"):
        _inspect(tmp_path / "again", archive, max_bytes=archive.stat().st_size - 1)


@pytest.mark.parametrize("damage", ["not gzip", "truncated"])
def test_an_unreadable_archive_is_rejected(tmp_path: Path, damage: str) -> None:
    archive = write_archive(tmp_path / "out" / ARCHIVE_NAME, openclaw_state(tmp_path / "state"))
    data = archive.read_bytes()
    _ = archive.write_bytes(b"plain bytes" if damage == "not gzip" else data[: len(data) // 2])
    with pytest.raises(SnapshotRejectedError):
        _inspect(tmp_path, archive)
