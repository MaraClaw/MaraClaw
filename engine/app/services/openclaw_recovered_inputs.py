import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, TypeAdapter, ValidationError

from app.core.json_types import JsonValue
from app.services.openclaw_transcript import MAX_EVENT_BYTES, _decode, open_transcript

MAX_ENTRY_CHARS: Final = 1024 * 1024
MAX_RECOVERY_EVENTS: Final = 1000
READ_SECONDS: Final = 2.0
_rows = TypeAdapter(list[tuple[str, int, str | None, bytes | None]])
_entries = TypeAdapter(list[tuple[str]])
type _RunId = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512)]


class _Flags(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)
    aborted: JsonValue = None
    error: JsonValue = None
    yielded: JsonValue = None
    continuation_pending: JsonValue = Field(default=None, alias="continuationPending")

    @property
    def clean(self) -> bool:
        return all(
            value is None or value is False
            for value in (self.aborted, self.error, self.yielded, self.continuation_pending)
        )


class _Evidence(_Flags):
    run_id: _RunId = Field(alias="runId")
    captured: StrictBool
    transcript_run_id: _RunId = Field(alias="transcriptRunId")
    meta: _Flags | None = None


class _Recovery(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)
    status: Literal["done"]
    terminal: tuple[_RunId, ...] = Field(alias="restartRecoveryTerminalRunIds")
    evidence: tuple[_Evidence, ...] = Field(alias="restartRecoveryTerminalDeliveryEvidence")
    source: None = Field(default=None, alias="restartRecoveryDeliverySourceRunId")
    delivery: None = Field(default=None, alias="restartRecoveryDeliveryRunId")
    recovery: None = Field(default=None, alias="restartRecoveryRunId")


@dataclass(frozen=True, slots=True)
class SuccessfulRecovery:
    source_run_id: UUID
    session_key: str
    transcript_run_id: str
    event_id: str


def successful_recovery(root: Path, session_key: str, message_id: UUID) -> SuccessfulRecovery | None:
    """Read tombstone and unique terminal event in one bounded read-only live-WAL snapshot, ignoring reply cursors."""
    try:
        with open_transcript(root) as source, closing(sqlite3.connect(source.uri, uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            deadline = time.monotonic() + READ_SECONDS
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            db.execute("BEGIN")
            tables = db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('session_nodes', 'session_windows', 'transcript_events')"
            ).fetchall()
            if len(tables) != 3:
                return None
            entries = _entries.validate_python(
                db.execute(
                    "SELECT entry_json FROM session_nodes WHERE session_key = ? AND length(entry_json) <= ? LIMIT 2",
                    (session_key, MAX_ENTRY_CHARS),
                ).fetchall()
            )
            if len(entries) != 1:
                return None
            entry = _Recovery.model_validate_json(entries[0][0])
            evidence = [item for item in entry.evidence if item.run_id == str(message_id)]
            if str(message_id) not in entry.terminal or len(evidence) != 1:
                return None
            captured = evidence[0]
            if not captured.captured or not captured.clean or (captured.meta is not None and not captured.meta.clean):
                return None
            rows = _rows.validate_python(
                db.execute(
                    "SELECT e.session_id, e.seq, CASE WHEN length(e.event_json) <= ? THEN e.event_json END, "
                    "CASE WHEN length(e.event_zstd) <= ? THEN e.event_zstd END FROM transcript_events e "
                    "WHERE EXISTS (SELECT 1 FROM session_windows w "
                    "WHERE w.session_id = e.session_id AND w.session_key = ?) LIMIT ?",
                    (MAX_EVENT_BYTES, MAX_EVENT_BYTES, session_key, MAX_RECOVERY_EVENTS + 1),
                ).fetchall()
            )
            if len(rows) > MAX_RECOVERY_EVENTS:
                return None
            matching = [
                row.event
                for row in _decode(rows)
                if row.event is not None and row.event.run_id == captured.transcript_run_id
            ]
            if len(matching) != 1:
                return None
            return SuccessfulRecovery(message_id, session_key, captured.transcript_run_id, matching[0].id)
    except FileNotFoundError, ValidationError:
        return None
