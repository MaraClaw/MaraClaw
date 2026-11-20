"""Last reconciled manifest row of one agent workspace path for the durable storage mirror."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import TypeAdapter

from app.core.json_types import datetime_from_row, int_from_row, str_from_row, str_from_row_opt, uuid_from_row

MirrorSkipReason = Literal["too_large"]

_skip_reason = TypeAdapter(MirrorSkipReason | None)


@dataclass(slots=True)
class WorkspaceMirrorEntryRecord:
    agent_id: UUID
    path: str
    generation: UUID
    local_ino: int
    local_size: int
    local_mtime_ns: int
    local_ctime_ns: int
    local_mode: int | None = None
    content_sha256: str | None = None
    remote_etag: str | None = None
    remote_size: int | None = None
    remote_token: str | None = None
    skip_reason: MirrorSkipReason | None = None
    synced_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> WorkspaceMirrorEntryRecord:
        local_mode = row.get("local_mode")
        remote_size = row.get("remote_size")
        return cls(
            agent_id=uuid_from_row(row["agent_id"]),
            path=str_from_row(row["path"]),
            generation=uuid_from_row(row["generation"]),
            local_ino=int_from_row(row["local_ino"]),
            local_size=int_from_row(row["local_size"]),
            local_mtime_ns=int_from_row(row["local_mtime_ns"]),
            local_ctime_ns=int_from_row(row["local_ctime_ns"]),
            local_mode=None if local_mode is None else int_from_row(local_mode),
            content_sha256=str_from_row_opt(row.get("content_sha256")),
            remote_etag=str_from_row_opt(row.get("remote_etag")),
            remote_size=None if remote_size is None else int_from_row(remote_size),
            remote_token=str_from_row_opt(row.get("remote_token")),
            skip_reason=_skip_reason.validate_python(row.get("skip_reason")),
            synced_at=datetime_from_row(row.get("synced_at")),
        )
