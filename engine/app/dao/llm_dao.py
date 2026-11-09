"""DAO for llm_models"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, ClassVar, final, override
from uuid import UUID, uuid4

from app.core.json_types import str_from_row, uuid_from_row, uuid_from_row_opt
from app.core.stored_credentials import StoredCredentialError, open_credential, seal_credential
from app.dao.base import BaseDAO
from app.dao.credential_contexts import model_key_context
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
    "oauth_account_id",
    "created_at",
    "updated_at",
)


@final
class LLMModelDAO(BaseDAO[LLMModelRecord]):
    """DAO for LLM model pool rows."""

    table: ClassVar[str] = "llm_models"
    columns: ClassVar[tuple[str, ...]] = _LLM_COLUMNS
    record_factory = staticmethod(LLMModelRecord.from_row)

    @override
    def _from_row(self, row: Mapping[str, object]) -> LLMModelRecord:
        record = self.record_factory(row)
        if record.auth_kind == "api_key" and record.api_key_encrypted:
            _ = open_credential(record.api_key_encrypted, model_key_context(record.tenant_id, record.id))
        return record

    @override
    def _deleted_row(self, row: Mapping[str, object]) -> LLMModelRecord:
        return replace(self.record_factory(row), api_key_encrypted="", refresh_token_encrypted=None)

    @override
    def _updated_row(self, row: Mapping[str, object]) -> LLMModelRecord:
        try:
            return self._from_row(row)
        except StoredCredentialError:
            return self._deleted_row(row)

    @override
    async def create(self, *, obj_in: Mapping[str, object]) -> LLMModelRecord:
        data = {**obj_in, "id": obj_in.get("id", uuid4())}
        if data.get("auth_kind", "api_key") == "api_key" and data.get("api_key_encrypted"):
            data["api_key_encrypted"] = seal_credential(
                str_from_row(data["api_key_encrypted"]),
                model_key_context(uuid_from_row_opt(data.get("tenant_id")), uuid_from_row(data["id"])),
            )
        return await super().create(obj_in=data)

    @override
    async def update(self, *, db_obj: LLMModelRecord, obj_in: Mapping[str, object]) -> LLMModelRecord:
        data = dict(obj_in)
        kind = str_from_row(data.get("auth_kind", db_obj.auth_kind))
        if kind == "api_key" and data.get("api_key_encrypted"):
            data["api_key_encrypted"] = seal_credential(
                str_from_row(data["api_key_encrypted"]),
                model_key_context(uuid_from_row_opt(data.get("tenant_id", db_obj.tenant_id)), db_obj.id),
            )
        return await super().update(db_obj=db_obj, obj_in=data)

    async def get_metadata(self, record_id: UUID) -> LLMModelRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM llm_models WHERE id = %(id)s", {"id": record_id}
            )
            return self._deleted_row(row) if row is not None else None

    async def list_metadata_for_tenant(self, tenant_id: UUID | None) -> Sequence[LLMModelRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM llm_models "
                + ("WHERE tenant_id = %(tenant_id)s " if tenant_id is not None else "")
                + "ORDER BY created_at DESC NULLS LAST",
                {"tenant_id": tenant_id},
            )
            return [self._deleted_row(row) for row in rows]

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
            return [self._from_row(row) for row in rows]

    async def first_enabled_id_for_tenant(self, tenant_id: UUID) -> UUID | None:
        async with self.session() as db:
            return uuid_from_row_opt(
                await db.fetchval(
                    "SELECT id FROM llm_models "
                    "WHERE tenant_id = %(tenant_id)s AND enabled IS TRUE "
                    "ORDER BY created_at ASC NULLS LAST LIMIT 1",
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
                    "WHERE tenant_id = %(tenant_id)s ORDER BY created_at DESC NULLS LAST",
                    {"tenant_id": tenant_id},
                )
            return [self._from_row(row) for row in rows]

    async def get_subscription_for_tenant(
        self, tenant_id: UUID, *, auth_kind: str = "grok_subscription"
    ) -> LLMModelRecord | None:
        """Return the company subscription row for ``auth_kind`` when one exists."""
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM llm_models "
                "WHERE tenant_id = %(tenant_id)s AND auth_kind = %(auth_kind)s "
                "ORDER BY updated_at DESC NULLS LAST LIMIT 1",
                {"tenant_id": tenant_id, "auth_kind": auth_kind},
            )
            return self._from_row(row) if row is not None else None


llm_model_dao = LLMModelDAO()
