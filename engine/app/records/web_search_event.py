"""Web search analytics event records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import (
    datetime_from_row,
    int_from_row,
    json_as_bool,
    str_from_row,
    uuid_from_row,
    uuid_from_row_opt,
)


@dataclass(slots=True)
class WebSearchEventRecord:
    """One billed Linkup call. No raw query, no response body."""

    id: UUID
    kind: str
    method: str
    http_status: int
    status_class: str
    latency_ms: int
    query_hash: str
    billed: bool = True
    query_normalized: str = ""
    query_char_len: int = 0
    schema_version: int = 1
    export_state: str = "skipped"
    agent_id: UUID | None = None
    tenant_id: UUID | None = None
    key_id: UUID | None = None
    depth: str | None = None
    output_type: str | None = None
    primary_domain: str | None = None
    result_count: int | None = None
    error_class: str | None = None
    upstream_job_id: str | None = None
    request_bytes: int | None = None
    response_bytes: int | None = None
    occurred_at: datetime | None = None
    export_claimed_at: datetime | None = None
    exported_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> WebSearchEventRecord:
        result_count = row.get("result_count")
        request_bytes = row.get("request_bytes")
        response_bytes = row.get("response_bytes")
        return cls(
            id=uuid_from_row(row["id"]),
            kind=str_from_row(row["kind"]),
            method=str_from_row(row["method"]),
            http_status=int_from_row(row["http_status"]),
            status_class=str_from_row(row["status_class"]),
            latency_ms=int_from_row(row["latency_ms"]),
            query_hash=str_from_row(row["query_hash"]),
            billed=json_as_bool(row.get("billed"), True),
            query_normalized=str_from_row(row.get("query_normalized"), ""),
            query_char_len=int_from_row(row.get("query_char_len"), 0),
            schema_version=int_from_row(row.get("schema_version"), 1),
            export_state=str_from_row(row.get("export_state"), "skipped"),
            agent_id=uuid_from_row_opt(row.get("agent_id")),
            tenant_id=uuid_from_row_opt(row.get("tenant_id")),
            key_id=uuid_from_row_opt(row.get("key_id")),
            depth=str_from_row(row.get("depth")) or None,
            output_type=str_from_row(row.get("output_type")) or None,
            primary_domain=str_from_row(row.get("primary_domain")) or None,
            result_count=int_from_row(result_count) if result_count is not None else None,
            error_class=str_from_row(row.get("error_class")) or None,
            upstream_job_id=str_from_row(row.get("upstream_job_id")) or None,
            request_bytes=int_from_row(request_bytes) if request_bytes is not None else None,
            response_bytes=int_from_row(response_bytes) if response_bytes is not None else None,
            occurred_at=datetime_from_row(row.get("occurred_at")),
            export_claimed_at=datetime_from_row(row.get("export_claimed_at")),
            exported_at=datetime_from_row(row.get("exported_at")),
        )
