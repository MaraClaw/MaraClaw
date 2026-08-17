"""DAO for llm_models (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, final
from uuid import UUID

from app.core.json_types import uuid_from_row_opt
from app.dao.base import BaseDAO
from app.records.llm import LLMModelRecord

_LLM_COLUMNS = (
    "id",
    "tenant_id",
    "provider",
    "model",
    "api_key_encrypted",
    "base_url",
    "label",
    "max_tokens_per_day",
    "enabled",
    "supports_vision",
    "temperature",
    "request_timeout",
    "max_output_tokens",
    "reasoning_effort",
    "auth_kind",
    "refresh_token_encrypted",
    "token_expires_at",
    "created_at",
    "updated_at",
)


@final
class LLMModelDAO(BaseDAO[LLMModelRecord]):
    """DAO for LLM model pool rows."""

    table: ClassVar[str] = "llm_models"
    columns: ClassVar[tuple[str, ...]] = _LLM_COLUMNS
    record_factory = staticmethod(LLMModelRecord.from_row)

    async def list_enabled(self, *, tenant_id: UUID | None = None) -> Sequence[LLMModelRecord]:
        params: dict[str, Any] = {}
        clauses = ["enabled = TRUE"]
        if tenant_id is not None:
            clauses.append("tenant_id = %(tenant_id)s")
            params["tenant_id"] = tenant_id
        where = " AND ".join(clauses)
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM llm_models WHERE {where} ORDER BY label ASC",
                params or None,
            )
            return [LLMModelRecord.from_row(row) for row in rows]

    async def first_enabled_id_for_tenant(self, tenant_id: UUID) -> UUID | None:
        async with self.session() as db:
            return uuid_from_row_opt(
                await db.fetchval(
                    "SELECT id FROM llm_models "
                    + "WHERE tenant_id = %(tenant_id)s AND enabled IS TRUE "
                    + "ORDER BY created_at ASC NULLS LAST LIMIT 1",
                    {"tenant_id": tenant_id},
                )
            )

    async def list_for_tenant(self, tenant_id: UUID | None) -> Sequence[LLMModelRecord]:
        """List models for a tenant (or all models when tenant_id is None)."""
        async with self.session() as db:
            if tenant_id is None:
                rows = await db.fetchall(
                    f"SELECT {self._select_list()} FROM llm_models ORDER BY created_at DESC NULLS LAST"
                )
            else:
                rows = await db.fetchall(
                    f"SELECT {self._select_list()} FROM llm_models "
                    + "WHERE tenant_id = %(tenant_id)s ORDER BY created_at DESC NULLS LAST",
                    {"tenant_id": tenant_id},
                )
            return [LLMModelRecord.from_row(row) for row in rows]

    async def get_subscription_for_tenant(
        self, tenant_id: UUID, *, auth_kind: str = "grok_subscription"
    ) -> LLMModelRecord | None:
        """Return the company Grok subscription row when one exists."""
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM llm_models "
                + "WHERE tenant_id = %(tenant_id)s AND auth_kind = %(auth_kind)s "
                + "ORDER BY updated_at DESC NULLS LAST LIMIT 1",
                {"tenant_id": tenant_id, "auth_kind": auth_kind},
            )
            return LLMModelRecord.from_row(row) if row else None


llm_model_dao = LLMModelDAO()
