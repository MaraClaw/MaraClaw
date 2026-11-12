"""Interruption evidence reader over a real live-WAL SQLite file shaped like OpenClaw 2026.11.3."""

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.services.openclaw_interrupted_inputs import interrupted_before_consumption

MESSAGE = uuid4()
RUN = str(MESSAGE)
KEY = f"agent:main:hook:maraclaw:{uuid4()}"

Entry = dict[str, object]
Row = dict[str, object]


def tombstone() -> Entry:
    return {
        "status": "done",
        "restartRecoveryTerminalRunIds": [RUN, "recovery-run"],
        "restartRecoveryTerminalDeliveryEvidence": [
            {"runId": RUN, "captured": True, "payloads": [{"visible": True}], "transcriptRunId": "recovery-run"}
        ],
        "restartRecoveryDeliverySourceRunId": None,
        "restartRecoveryDeliveryRunId": None,
    }


def pending() -> Row:
    return {"session_id": "window", "idempotency_key": f"{RUN}:user", "run_id": RUN, "state": "interrupted"}


def open_guest(root: Path, *, tables: bool = True) -> sqlite3.Connection:
    path = root / "agents/main/agent/openclaw-agent.sqlite"
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    _ = db.execute("PRAGMA journal_mode=WAL")
    if tables:
        _ = db.execute(
            "CREATE TABLE session_nodes (session_key TEXT NOT NULL PRIMARY KEY, current_session_id TEXT NOT NULL, "
            "entry_json TEXT NOT NULL) STRICT"
        )
        _ = db.execute(
            "CREATE TABLE session_pending_inputs (seq INTEGER PRIMARY KEY AUTOINCREMENT, session_key TEXT NOT NULL, "
            "session_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, run_id TEXT NOT NULL, state TEXT NOT NULL, "
            "consumed_event_id TEXT, UNIQUE (session_id, idempotency_key)) STRICT"
        )
    return db


def seed(db: sqlite3.Connection, entry: Entry | str, rows: list[Row], key: str = KEY) -> None:
    text = entry if isinstance(entry, str) else json.dumps(entry)
    _ = db.execute("INSERT INTO session_nodes VALUES (?, 'window', ?)", (key, text))
    for row in rows:
        _ = db.execute(
            "INSERT INTO session_pending_inputs (session_key, session_id, idempotency_key, run_id, state, "
            "consumed_event_id) VALUES (?, ?, ?, ?, ?, ?)",
            (key, row["session_id"], row["idempotency_key"], row["run_id"], row["state"], row.get("consumed")),
        )
    db.commit()


def read(root: Path, key: str = KEY, message: UUID = MESSAGE) -> bool:
    return interrupted_before_consumption(root, key, message)


def test_exact_tombstone_and_interrupted_unconsumed_input_is_evidence_over_a_live_wal(tmp_path: Path) -> None:
    db = open_guest(tmp_path)
    try:
        seed(db, tombstone(), [pending()])
        assert (tmp_path / "agents/main/agent/openclaw-agent.sqlite-wal").stat().st_size > 0
        assert read(tmp_path) is True
    finally:
        db.close()


def drop(key: str) -> Callable[[Entry], None]:
    def mutate(entry: Entry) -> None:
        _ = entry.pop(key)

    return mutate


def put(key: str, value: object) -> Callable[[Entry], None]:
    return lambda entry: entry.__setitem__(key, value)


def evidence(change: Callable[[Row], None]) -> Callable[[Entry], None]:
    def mutate(entry: Entry) -> None:
        items = entry["restartRecoveryTerminalDeliveryEvidence"]
        assert isinstance(items, list)
        change(items[0])

    return mutate


def duplicate_evidence(entry: Entry) -> None:
    items = entry["restartRecoveryTerminalDeliveryEvidence"]
    assert isinstance(items, list)
    items.append(dict(items[0]))


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(put("status", "running"), id="not-terminal"),
        pytest.param(drop("status"), id="status-missing"),
        pytest.param(put("restartRecoveryTerminalRunIds", ["recovery-run"]), id="run-not-tombstoned"),
        pytest.param(put("restartRecoveryTerminalRunIds", RUN), id="ids-not-array"),
        pytest.param(drop("restartRecoveryTerminalRunIds"), id="ids-missing"),
        pytest.param(put("restartRecoveryTerminalDeliveryEvidence", []), id="no-evidence"),
        pytest.param(drop("restartRecoveryTerminalDeliveryEvidence"), id="evidence-missing"),
        pytest.param(duplicate_evidence, id="two-evidence-items"),
        pytest.param(evidence(lambda item: item.__setitem__("runId", "other")), id="evidence-for-other-run"),
        pytest.param(evidence(lambda item: item.__setitem__("captured", False)), id="not-captured"),
        pytest.param(evidence(lambda item: item.__setitem__("captured", "true")), id="captured-not-boolean"),
        pytest.param(evidence(lambda item: item.__setitem__("aborted", True)), id="aborted"),
        pytest.param(evidence(lambda item: item.__setitem__("error", "boom")), id="error"),
        pytest.param(evidence(lambda item: item.__setitem__("meta", {"yielded": True})), id="yielded-meta"),
        pytest.param(
            evidence(lambda item: item.__setitem__("meta", {"continuationPending": True})), id="continuation-pending"
        ),
        pytest.param(put("restartRecoveryDeliverySourceRunId", "recovery-run"), id="recovery-source-active"),
        pytest.param(put("restartRecoveryDeliveryRunId", "recovery-run"), id="recovery-delivery-active"),
    ],
)
def test_tombstone_that_is_not_exact_terminal_evidence_is_refused(
    tmp_path: Path, mutate: Callable[[Entry], None]
) -> None:
    entry = tombstone()
    mutate(entry)
    db = open_guest(tmp_path)
    try:
        seed(db, entry, [pending()])
        assert read(tmp_path) is False
    finally:
        db.close()


@pytest.mark.parametrize(
    "row",
    [
        {"state": "queued"},
        {"state": "cancelled"},
        {"consumed": "event-1"},
        {"run_id": str(uuid4())},
        {"idempotency_key": f"{uuid4()}:user"},
        {"idempotency_key": RUN},
    ],
    ids=["queued", "cancelled", "consumed", "other-run", "other-idempotency", "unsuffixed-idempotency"],
)
def test_pending_row_that_is_not_the_exact_unconsumed_interruption_is_refused(tmp_path: Path, row: Row) -> None:
    db = open_guest(tmp_path)
    try:
        seed(db, tombstone(), [pending() | row])
        assert read(tmp_path) is False
    finally:
        db.close()


def test_pending_row_alone_without_the_tombstone_is_refused(tmp_path: Path) -> None:
    db = open_guest(tmp_path)
    try:
        seed(db, {"status": "running"}, [pending()])
        assert read(tmp_path) is False
    finally:
        db.close()


def test_duplicate_matching_rows_across_windows_are_ambiguous(tmp_path: Path) -> None:
    db = open_guest(tmp_path)
    try:
        seed(db, tombstone(), [pending(), pending() | {"session_id": "older-window"}])
        assert read(tmp_path) is False
    finally:
        db.close()


def test_evidence_under_another_session_key_or_message_is_refused(tmp_path: Path) -> None:
    db = open_guest(tmp_path)
    try:
        seed(db, tombstone(), [pending()])
        assert read(tmp_path, key=f"agent:main:hook:maraclaw:{uuid4()}") is False
        assert read(tmp_path, message=uuid4()) is False
    finally:
        db.close()


@pytest.mark.parametrize(
    "entry", ["not json", "[]", '"done"', "x" * 1_048_577], ids=["text", "array", "string", "oversize"]
)
def test_malformed_or_unbounded_entry_json_is_refused(tmp_path: Path, entry: str) -> None:
    db = open_guest(tmp_path)
    try:
        seed(db, entry, [pending()])
        assert read(tmp_path) is False
    finally:
        db.close()


def test_missing_schema_or_database_is_refused(tmp_path: Path) -> None:
    assert read(tmp_path / "absent") is False
    db = open_guest(tmp_path / "empty", tables=False)
    try:
        _ = db.execute("CREATE TABLE session_pending_inputs (run_id TEXT)")
        db.commit()
        assert read(tmp_path / "empty") is False
    finally:
        db.close()


def test_unexpected_column_layout_raises_instead_of_claiming_evidence(tmp_path: Path) -> None:
    db = open_guest(tmp_path, tables=False)
    try:
        _ = db.execute("CREATE TABLE session_nodes (session_key TEXT, entry_json TEXT)")
        _ = db.execute("CREATE TABLE session_pending_inputs (run_id TEXT)")
        _ = db.execute("INSERT INTO session_nodes VALUES (?, ?)", (KEY, json.dumps(tombstone())))
        db.commit()
        with pytest.raises(sqlite3.OperationalError):
            _ = read(tmp_path)
    finally:
        db.close()
