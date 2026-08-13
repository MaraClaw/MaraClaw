"""DAO for llm_models (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

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
    "created_at",
    "updated_at",
)


class LLMModelDAO(BaseDAO[LLMModelRecord]):
    """DAO for LLM model pool rows."""

    table = "llm_models"
    columns = _LLM_COLUMNS
    record_factory = staticmethod(LLMModelRecord.from_row)

    async def list_enabled(self, *, tenant_id: UUID | None = None) -> Sequence[LLMModelRecord]:
        params: dict[str, Any] = {}
        clauses = ["enabled = TRUE"]
        if tenant_id is not None:
            clauses.append("(tenant_id IS NULL OR tenant_id = %(tenant_id)s)")
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
            return await db.fetchval(
                "SELECT id FROM llm_models "
                "WHERE tenant_id = %(tenant_id)s AND enabled IS TRUE "
                "ORDER BY created_at ASC NULLS LAST LIMIT 1",
                {"tenant_id": tenant_id},
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
                    "WHERE tenant_id = %(tenant_id)s ORDER BY created_at DESC NULLS LAST",
                    {"tenant_id": tenant_id},
                )
            return [LLMModelRecord.from_row(row) for row in rows]


llm_model_dao = LLMModelDAO()
