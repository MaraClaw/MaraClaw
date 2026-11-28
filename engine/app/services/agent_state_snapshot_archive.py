"""Checks a fresh state snapshot must pass before it is published (docs/agent-state-snapshots.md).

The backup must report one verified state asset without the workspace; the archive must stay under the size limit,
hold no unfinished SQLite journal, carry databases that pass `integrity_check`, and show no turn OpenClaw would
resume after a restart.
"""

import gzip
import sqlite3
import tarfile
import time
import zlib
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Final

from pydantic import TypeAdapter

from app.core.json_types import is_any_list, is_str_dict, json_loads_value
from app.services import agent_state_snapshot as snapshots
from app.services.agent_state_snapshot import AGENT_DB, GUEST_STATE_DIR
from app.services.workspace_mirror_scope import MAX_DEPTH

ASSET_PAYLOAD: Final = "payload/posix" + GUEST_STATE_DIR
_PAYLOAD_PARTS = PurePosixPath(ASSET_PAYLOAD).parts
# Databases are extracted to be checked; this bounds the disk the extraction takes.
SQLITE_MAX_BYTES: Final = 8 * 1024**3
CHECK_SECONDS: Final = 120.0
_PROGRESS_STEPS = 1000
# OpenClaw resumes a session after a restart when its entry sets any of these keys (JavaScript truthiness, which
# Python's matches for the values json_extract returns).
_RESUMABLE_SQL = (
    "SELECT json_extract(entry_json, '$.lifecycleRunId'), json_extract(entry_json, '$.resumeAfterRestart'), "
    "json_extract(entry_json, '$.mainRestartRecovery') FROM session_nodes WHERE instr(entry_json, 'lifecycleRunId') "
    "OR instr(entry_json, 'resumeAfterRestart') OR instr(entry_json, 'mainRestartRecovery')"
)
_rows = TypeAdapter(list[tuple[object, ...]])


class SnapshotRejectedError(Exception):
    """The backup or its archive is not a publishable snapshot; the previous snapshot stays current."""


def check_backup_report(stdout: str) -> None:
    """Accept only the report of a verified backup of the one agent state directory, the workspace left out."""
    start = stdout.find("{")
    if start < 0:
        raise SnapshotRejectedError("The backup printed no report")
    try:
        report = json_loads_value(stdout[start:])
    except ValueError as exc:
        raise SnapshotRejectedError("The backup report is not JSON") from exc
    if not is_str_dict(report):
        raise SnapshotRejectedError("The backup report is not an object")
    if report.get("verified") is not True:
        raise SnapshotRejectedError("The backup was not verified")
    if report.get("includeWorkspace") is not False:
        raise SnapshotRejectedError("The backup included the workspace")
    assets = report.get("assets")
    if not is_any_list(assets) or len(assets) != 1 or not is_str_dict(asset := assets[0]):
        raise SnapshotRejectedError("The backup does not hold exactly one asset")
    archive_path = asset.get("archivePath")
    if (
        asset.get("kind") != "state"
        or asset.get("sourcePath") != GUEST_STATE_DIR
        or not isinstance(archive_path, str)
        or not archive_path.endswith(f"/{ASSET_PAYLOAD}")
    ):
        raise SnapshotRejectedError("The backup asset is not the agent state directory")


def inspect_archive(archive: Path, scratch: Path, max_bytes: int) -> None:
    """Raise `SnapshotRejectedError` unless the archive is a bounded, quiescent snapshot whose databases are intact.

    Blocking. Every `*.sqlite` member is extracted below the empty private directory `scratch` to be checked.
    """
    size = archive.stat().st_size
    if size > max_bytes:
        raise SnapshotRejectedError(f"The archive is {size} bytes, over the {max_bytes} byte limit")
    deadline = time.monotonic() + CHECK_SECONDS
    databases: list[Path] = []
    agent_db: Path | None = None
    extracted = 0
    entries: set[str] = set()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                if time.monotonic() >= deadline:
                    raise SnapshotRejectedError("The archive checks ran out of time")
                _count_payload_entry(member, entries)
                name = member.name
                if not member.isfile():
                    continue
                if name.endswith(("-wal", "-journal")) and member.size > 0:
                    raise SnapshotRejectedError(f"The archive holds an unfinished SQLite journal: {name}")
                if not name.endswith(".sqlite"):
                    continue
                extracted += member.size
                if extracted > SQLITE_MAX_BYTES:
                    raise SnapshotRejectedError("The archive's databases exceed the extraction limit")
                tar.extract(member, scratch, filter="data")
                databases.append(scratch / name)
                if name.endswith(f"/{ASSET_PAYLOAD}/{AGENT_DB}"):
                    agent_db = scratch / name
    except (tarfile.TarError, EOFError, zlib.error, gzip.BadGzipFile) as exc:
        raise SnapshotRejectedError(f"The archive is unreadable: {type(exc).__name__}") from exc
    for database in databases:
        _check_integrity(database, deadline)
    if agent_db is not None:
        _check_quiescent(agent_db, deadline)


def _count_payload_entry(member: tarfile.TarInfo, entries: set[str]) -> None:
    """Count what placement walks, including implicit directories, rather than trusting the live source tree."""
    parts = PurePosixPath(member.name).parts
    start = 1 + len(_PAYLOAD_PARTS)
    if parts[1:start] != _PAYLOAD_PARTS or len(parts) <= start:
        return
    payload = parts[start:]
    if not snapshots.covered_directory(payload[0]):
        payload = payload[:1]
    if len(payload) - int(not member.isdir()) > MAX_DEPTH:
        raise SnapshotRejectedError("The archived state exceeds the placement limit for directory depth")
    entry = "/".join(payload)
    while entry and entry not in entries:
        entries.add(entry)
        if len(entries) > snapshots.WALK_MAX_ENTRIES:
            raise SnapshotRejectedError("The archived state exceeds the placement limit for entries")
        entry = entry.rpartition("/")[0]


def _connect(database: Path, deadline: float) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro&immutable=1", uri=True)
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), _PROGRESS_STEPS)
    _ = connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def _check_integrity(database: Path, deadline: float) -> None:
    try:
        with closing(_connect(database, deadline)) as connection:
            rows = _rows.validate_python(connection.execute("PRAGMA integrity_check").fetchall())
    except sqlite3.Error as exc:
        raise SnapshotRejectedError(f"{database.name} could not be checked: {type(exc).__name__}") from exc
    if rows != [("ok",)]:
        raise SnapshotRejectedError(f"{database.name} failed its integrity check")


def _check_quiescent(database: Path, deadline: float) -> None:
    """Reject a snapshot of a session mid-run: restored, the gateway would resume it as a ghost turn."""
    try:
        with closing(_connect(database, deadline)) as connection:
            tables = {
                row[0]
                for row in _rows.validate_python(
                    connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'").fetchall()
                )
            }
            pending = (
                _rows.validate_python(connection.execute("SELECT count(*) FROM session_pending_inputs").fetchall())
                if "session_pending_inputs" in tables
                else [(0,)]
            )
            flags = (
                _rows.validate_python(connection.execute(_RESUMABLE_SQL).fetchall())
                if "session_nodes" in tables
                else []
            )
    except sqlite3.Error as exc:
        raise SnapshotRejectedError(f"{database.name} could not be checked: {type(exc).__name__}") from exc
    if pending != [(0,)]:
        raise SnapshotRejectedError("A session input was still pending")
    if any(any(row) for row in flags):
        raise SnapshotRejectedError("A session was mid-run")
