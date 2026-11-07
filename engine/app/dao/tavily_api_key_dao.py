"""DAO for Tavily API key ring tables"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import ClassVar, final
from uuid import UUID

from app.core.json_types import int_from_row, uuid_from_row_opt
from app.dao.base import BaseDAO
from app.records.tavily_api_key import TavilyApiKeyRecord

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


@final
class TavilyApiKeyDAO(BaseDAO[TavilyApiKeyRecord]):
    """Stored Tavily API keys (platform pool)."""

    table: ClassVar[str] = "tavily_api_keys"
    columns: ClassVar[tuple[str, ...]] = _KEY_COLUMNS
    record_factory = staticmethod(TavilyApiKeyRecord.from_row)

    async def get_by_fingerprint(self, fingerprint: str) -> TavilyApiKeyRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM tavily_api_keys WHERE key_fingerprint = %(fingerprint)s",
                {"fingerprint": fingerprint},
            )
            return TavilyApiKeyRecord.from_row(row) if row is not None else None

    async def max_position(self) -> int | None:
        async with self.session() as db:
            value = await db.fetchval("SELECT MAX(position) FROM tavily_api_keys")
            if value is None:
                return None
            return int_from_row(value)

    async def list_ordered(self) -> Sequence[TavilyApiKeyRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tavily_api_keys ORDER BY position ASC, created_at ASC"
            )
            return [TavilyApiKeyRecord.from_row(row) for row in rows]

    async def list_active_ordered(self, *, now: datetime) -> Sequence[TavilyApiKeyRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tavily_api_keys "
                "WHERE status = 'active' AND (exhausted_until IS NULL OR exhausted_until <= %(now)s) "
                "ORDER BY position ASC, created_at ASC",
                {"now": now},
            )
            return [TavilyApiKeyRecord.from_row(row) for row in rows]

    async def get_cursor_key_id(self) -> UUID | None:
        async with self.session() as db:
            value = await db.fetchval("SELECT current_key_id FROM tavily_key_ring_state WHERE id = 1")
            return uuid_from_row_opt(value)

    async def set_cursor_key_id(self, key_id: UUID | None) -> None:
        async with self.session() as db:
            _ = await db.execute(
                "INSERT INTO tavily_key_ring_state (id, current_key_id, updated_at) "
                "VALUES (1, %(key_id)s, NOW()) "
                "ON CONFLICT (id) DO UPDATE SET current_key_id = EXCLUDED.current_key_id, updated_at = NOW()",
                {"key_id": key_id},
            )


tavily_api_key_dao = TavilyApiKeyDAO()
