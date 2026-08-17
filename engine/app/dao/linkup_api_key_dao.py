"""DAO for Linkup API key ring tables"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import ClassVar, final
from uuid import UUID

from app.core.json_types import int_from_row, uuid_from_row_opt
from app.dao.base import BaseDAO
from app.records.linkup_api_key import LinkupApiKeyRecord, LinkupAsyncJobRecord

_KEY_COLUMNS = (
    "id",
    "tenant_id",
    "label",
    "key_ciphertext",
    "key_fingerprint",
    "position",
    "status",
    "exhausted_until",
    "last_error",
    "last_used_at",
    "created_at",
    "updated_at",
)

_JOB_COLUMNS = ("upstream_job_id", "key_id", "kind", "created_at")


@final
class LinkupApiKeyDAO(BaseDAO[LinkupApiKeyRecord]):
    """Stored Linkup API keys (platform pool)."""

    table: ClassVar[str] = "linkup_api_keys"
    columns: ClassVar[tuple[str, ...]] = _KEY_COLUMNS
    record_factory = staticmethod(LinkupApiKeyRecord.from_row)

    async def get_by_fingerprint(self, fingerprint: str) -> LinkupApiKeyRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM linkup_api_keys WHERE key_fingerprint = %(fingerprint)s",
                {"fingerprint": fingerprint},
            )
            return LinkupApiKeyRecord.from_row(row) if row else None

    async def max_position(self) -> int | None:
        async with self.session() as db:
            value = await db.fetchval("SELECT MAX(position) FROM linkup_api_keys")
            if value is None:
                return None
            return int_from_row(value)

    async def list_ordered(self) -> Sequence[LinkupApiKeyRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM linkup_api_keys ORDER BY position ASC, created_at ASC"
            )
            return [LinkupApiKeyRecord.from_row(row) for row in rows]

    async def list_active_ordered(self, *, now: datetime) -> Sequence[LinkupApiKeyRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM linkup_api_keys "
                + "WHERE status = 'active' AND (exhausted_until IS NULL OR exhausted_until <= %(now)s) "
                + "ORDER BY position ASC, created_at ASC",
                {"now": now},
            )
            return [LinkupApiKeyRecord.from_row(row) for row in rows]

    async def get_cursor_key_id(self) -> UUID | None:
        async with self.session() as db:
            value = await db.fetchval("SELECT current_key_id FROM linkup_key_ring_state WHERE id = 1")
            return uuid_from_row_opt(value)

    async def set_cursor_key_id(self, key_id: UUID | None) -> None:
        async with self.session() as db:
            _ = await db.execute(
                "INSERT INTO linkup_key_ring_state (id, current_key_id, updated_at) "
                + "VALUES (1, %(key_id)s, NOW()) "
                + "ON CONFLICT (id) DO UPDATE SET current_key_id = EXCLUDED.current_key_id, updated_at = NOW()",
                {"key_id": key_id},
            )


@final
class LinkupAsyncJobDAO(BaseDAO[LinkupAsyncJobRecord]):
    """Bind async Linkup jobs to the creating key."""

    table: ClassVar[str] = "linkup_async_jobs"
    pk: ClassVar[str] = "upstream_job_id"
    columns: ClassVar[tuple[str, ...]] = _JOB_COLUMNS
    record_factory = staticmethod(LinkupAsyncJobRecord.from_row)

    async def get_by_job_id(self, upstream_job_id: str) -> LinkupAsyncJobRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM linkup_async_jobs " + "WHERE upstream_job_id = %(upstream_job_id)s",
                {"upstream_job_id": upstream_job_id},
            )
            return LinkupAsyncJobRecord.from_row(row) if row else None

    async def delete_by_job_id(self, upstream_job_id: str) -> None:
        async with self.session() as db:
            _ = await db.execute(
                "DELETE FROM linkup_async_jobs WHERE upstream_job_id = %(upstream_job_id)s",
                {"upstream_job_id": upstream_job_id},
            )


linkup_api_key_dao = LinkupApiKeyDAO()
linkup_async_job_dao = LinkupAsyncJobDAO()
