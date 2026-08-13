"""DAO for gogcli_credential_states (psycopg)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.dao.base import BaseDAO
from app.records.gogcli_credential import GogcliCredentialStateRecord

_COLUMNS = (
    "id",
    "agent_id",
    "encrypted_keyring_password",
    "encrypted_gog_data_archive",
    "account_hint",
    "status",
    "keyring_password_updated_at",
    "credential_snapshot_updated_at",
    "last_authenticated_at",
    "last_status_checked_at",
    "last_restored_at",
    "created_at",
    "updated_at",
)


class GogcliCredentialStateDAO(BaseDAO[GogcliCredentialStateRecord]):
    table = "gogcli_credential_states"
    columns = _COLUMNS
    record_factory = staticmethod(GogcliCredentialStateRecord.from_row)

    async def get_by_agent(self, agent_id: UUID) -> GogcliCredentialStateRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM gogcli_credential_states WHERE agent_id = %(agent_id)s LIMIT 1",
                {"agent_id": agent_id},
            )
            return GogcliCredentialStateRecord.from_row(row) if row else None

    async def upsert_fields(self, agent_id: UUID, fields: dict[str, Any]) -> GogcliCredentialStateRecord:
        existing = await self.get_by_agent(agent_id)
        if existing:
            return await self.update(db_obj=existing, obj_in=fields)
        data = {"agent_id": agent_id, **fields}
        return await self.create(obj_in=data)


gogcli_credential_state_dao = GogcliCredentialStateDAO()
