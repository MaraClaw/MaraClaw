"""Exact durable guest evidence that a delivered native turn was interrupted before its input was consumed."""

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, Json, StrictBool, TypeAdapter, ValidationError

from app.services.openclaw_transcript import open_transcript

MAX_ENTRY_CHARS = 1024 * 1024
READ_SECONDS = 2.0
_FIELDS = ("status", "terminal", "evidence", "source", "delivery")
_rows = TypeAdapter(list[tuple[object, ...]])


class _Flags(BaseModel):
    aborted: object = None
    error: object = None
    yielded: object = None
    continuation_pending: object = Field(default=None, alias="continuationPending")

    @property
    def clean(self) -> bool:
        return all(
            flag is None or flag is False
            for flag in (self.aborted, self.error, self.yielded, self.continuation_pending)
        )


class _Evidence(_Flags):
    run_id: str = Field(alias="runId")
    captured: StrictBool
    meta: _Flags | None = None


class _Entry(BaseModel):
    """The restart-recovery tombstone fields of ``session_nodes.entry_json``; any other shape is not evidence."""

    status: Literal["done"]
    terminal: Json[list[str]]
    evidence: Json[list[_Evidence]]
    source: None
    delivery: None


def _terminal_recovery(db: sqlite3.Connection, session_key: str, run_id: str) -> bool:
    rows = _rows.validate_python(
        db.execute(
            "SELECT json_extract(entry_json, '$.status'), json_extract(entry_json, '$.restartRecoveryTerminalRunIds'), "
            "json_extract(entry_json, '$.restartRecoveryTerminalDeliveryEvidence'), "
            "json_extract(entry_json, '$.restartRecoveryDeliverySourceRunId'), "
            "json_extract(entry_json, '$.restartRecoveryDeliveryRunId') FROM session_nodes "
            "WHERE session_key = ? AND length(entry_json) <= ? AND json_valid(entry_json) LIMIT 2",
            (session_key, MAX_ENTRY_CHARS),
        ).fetchall()
    )
    if len(rows) != 1:
        return False
    entry = _Entry.model_validate(dict(zip(_FIELDS, rows[0], strict=True)))
    matching = [item for item in entry.evidence if item.run_id == run_id]
    return (
        run_id in entry.terminal
        and len(matching) == 1
        and matching[0].captured
        and matching[0].clean
        and (matching[0].meta is None or matching[0].meta.clean)
    )


def _unconsumed_interruption(db: sqlite3.Connection, session_key: str, run_id: str) -> bool:
    rows = _rows.validate_python(
        db.execute(
            "SELECT state, consumed_event_id FROM session_pending_inputs "
            "WHERE session_key = ? AND run_id = ? AND idempotency_key = ? LIMIT 2",
            (session_key, run_id, f"{run_id}:user"),
        ).fetchall()
    )
    return len(rows) == 1 and rows[0][0] == "interrupted" and rows[0][1] is None


def interrupted_before_consumption(root: Path, session_key: str, message_id: UUID) -> bool:
    """True only for a terminal restart recovery of this exact run plus its one interrupted, unconsumed input row.

    Anything missing, ambiguous, malformed or unreadable by schema is ``False``; I/O and SQLite failures propagate.
    """
    run_id = str(message_id)
    try:
        with open_transcript(root) as source, closing(sqlite3.connect(source.uri, uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            deadline = time.monotonic() + READ_SECONDS
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            tables = _rows.validate_python(
                db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('session_nodes', 'session_pending_inputs')"
                ).fetchall()
            )
            if len(tables) != 2:
                return False
            return _terminal_recovery(db, session_key, run_id) and _unconsumed_interruption(db, session_key, run_id)
    except FileNotFoundError, ValidationError:
        return False
