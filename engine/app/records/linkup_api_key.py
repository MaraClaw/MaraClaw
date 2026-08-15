"""Linkup API key ring records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, int_from_row, str_from_row, uuid_from_row, uuid_from_row_opt


@dataclass(slots=True)
class LinkupApiKeyRecord:
    """One stored Linkup API key. Ciphertext is for DAO/service use only."""

    id: UUID
    label: str
    key_ciphertext: str
    key_fingerprint: str
    position: int
    status: str = "active"
    tenant_id: UUID | None = None
    exhausted_until: datetime | None = None
    last_error: str | None = None
    last_used_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> LinkupApiKeyRecord:
        last_error = row.get("last_error")
        return cls(
            id=uuid_from_row(row["id"]),
            label=str_from_row(row["label"]),
            key_ciphertext=str_from_row(row["key_ciphertext"]),
            key_fingerprint=str_from_row(row["key_fingerprint"]),
            position=int_from_row(row.get("position")),
            status=str_from_row(row.get("status"), "active"),
            tenant_id=uuid_from_row_opt(row.get("tenant_id")),
            exhausted_until=datetime_from_row(row.get("exhausted_until")),
            last_error=str(last_error) if last_error is not None else None,
            last_used_at=datetime_from_row(row.get("last_used_at")),
            created_at=datetime_from_row(row.get("created_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
        )


@dataclass(slots=True)
class LinkupAsyncJobRecord:
    """Bind an async Linkup research/extract job to the key that created it."""

    upstream_job_id: str
    key_id: UUID
    kind: str
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> LinkupAsyncJobRecord:
        return cls(
            upstream_job_id=str_from_row(row["upstream_job_id"]),
            key_id=uuid_from_row(row["key_id"]),
            kind=str_from_row(row["kind"]),
            created_at=datetime_from_row(row.get("created_at")),
        )
