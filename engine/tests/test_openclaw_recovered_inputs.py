import json
import sqlite3
from collections.abc import Iterator
from compression import zstd
from pathlib import Path

import pytest

from app.services.openclaw_recovered_inputs import successful_recovery
from test_openclaw_delayed_replies import append
from test_openclaw_interrupted_inputs import KEY, MESSAGE, open_guest, seed, tombstone


@pytest.fixture
def recovered_guest(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = open_guest(tmp_path)
    db.execute("CREATE TABLE session_windows (session_id TEXT, session_key TEXT)")
    db.execute(
        "CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, event_json TEXT, event_zstd BLOB, message_role TEXT, created_at INTEGER)"
    )
    db.execute("INSERT INTO session_windows VALUES ('window', ?)", (KEY,))
    seed(db, tombstone(), [])
    append(db, "window", 20, run="recovery-run", text="Recovered answer", event_id="terminal-event")
    try:
        yield db
    finally:
        db.close()


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("stop", ["stop", "end_turn"])
def test_exact_successful_witness_reads_live_wal_without_pending_input_or_cursor(
    recovered_guest: sqlite3.Connection, tmp_path: Path, compressed: bool, stop: str
) -> None:
    db = recovered_guest
    envelope = json.loads(db.execute("SELECT event_json FROM transcript_events").fetchone()[0])
    envelope["message"]["stopReason"] = stop
    db.execute("UPDATE transcript_events SET event_json = ?", (json.dumps(envelope),))
    db.commit()
    if compressed:
        raw = db.execute("SELECT event_json FROM transcript_events").fetchone()[0]
        db.execute("UPDATE transcript_events SET event_json = NULL, event_zstd = ?", (zstd.compress(raw.encode()),))
        db.commit()
    assert (tmp_path / "agents/main/agent/openclaw-agent.sqlite-wal").stat().st_size > 0

    witness = successful_recovery(tmp_path, KEY, MESSAGE)

    assert witness is not None
    assert (witness.source_run_id, witness.session_key, witness.transcript_run_id, witness.event_id) == (
        MESSAGE,
        KEY,
        "recovery-run",
        "terminal-event",
    )


@pytest.mark.parametrize(
    "change",
    [
        "status",
        "source_run",
        "duplicate_evidence",
        "uncaptured",
        "aborted",
        "error",
        "yielded",
        "continuation",
        "source_active",
        "delivery_active",
        "recovery_active",
        "missing_transcript_run",
        "wrong_transcript_run",
        "wrong_session",
        "duplicate_event",
        "malformed_entry",
        "malformed_event",
        "nonterminal",
        "nonassistant",
        "oversize_entry",
        "oversize_event",
        "bounded_scan",
    ],
)
def test_incomplete_ambiguous_or_malformed_success_evidence_is_refused(
    recovered_guest: sqlite3.Connection, tmp_path: Path, change: str
) -> None:
    db = recovered_guest
    entry = tombstone()
    items = entry["restartRecoveryTerminalDeliveryEvidence"]
    assert isinstance(items, list)
    item = items[0]
    raw = db.execute("SELECT event_json FROM transcript_events").fetchone()[0]
    event = json.loads(raw)
    match change:
        case "status":
            entry["status"] = "running"
        case "source_run":
            entry["restartRecoveryTerminalRunIds"] = ["recovery-run"]
        case "duplicate_evidence":
            items.append(dict(item))
        case "uncaptured":
            item["captured"] = False
        case "aborted" | "error" | "yielded":
            item[change] = True
        case "continuation":
            item["meta"] = {"continuationPending": True}
        case "source_active":
            entry["restartRecoveryDeliverySourceRunId"] = "active"
        case "delivery_active":
            entry["restartRecoveryDeliveryRunId"] = "active"
        case "recovery_active":
            entry["restartRecoveryRunId"] = "active"
        case "missing_transcript_run":
            del item["transcriptRunId"]
        case "wrong_transcript_run":
            item["transcriptRunId"] = "foreign-run"
        case "wrong_session":
            db.execute("UPDATE session_windows SET session_key = 'foreign'")
        case "duplicate_event":
            append(db, "window", 21, run="recovery-run")
        case "nonterminal":
            event["message"]["stopReason"] = "toolUse"
        case "nonassistant":
            event["message"]["role"] = "user"
        case "bounded_scan":
            db.executemany(
                "INSERT INTO transcript_events VALUES ('window', ?, '{}', NULL, 'tool', 0)", ((n,) for n in range(1001))
            )
    db.execute(
        "UPDATE session_nodes SET entry_json = ?",
        (
            "not json"
            if change == "malformed_entry"
            else "x" * 1_048_577
            if change == "oversize_entry"
            else json.dumps(entry),
        ),
    )
    db.execute(
        "UPDATE transcript_events SET event_json = ? WHERE seq = 20",
        (
            "not json"
            if change == "malformed_event"
            else "x" * 2_097_153
            if change == "oversize_event"
            else json.dumps(event),
        ),
    )
    db.commit()

    assert successful_recovery(tmp_path, KEY, MESSAGE) is None


def test_missing_guest_database_is_not_success_evidence(tmp_path: Path) -> None:
    assert successful_recovery(tmp_path / "absent", KEY, MESSAGE) is None
