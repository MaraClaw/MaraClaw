"""DAO for agent_credentials (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from app.dao.base import BaseDAO
from app.records.agent_credential import AgentCredentialRecord

_COLUMNS = (
    "id",
    "agent_id",
    "credential_type",
    "platform",
    "display_name",
    "cookies_json",
    "cookies_updated_at",
    "status",
    "last_login_at",
    "last_injected_at",
    "created_at",
    "updated_at",
)


class AgentCredentialDAO(BaseDAO[AgentCredentialRecord]):
    table = "agent_credentials"
    columns = _COLUMNS
    record_factory = staticmethod(AgentCredentialRecord.from_row)

    async def list_for_agent(self, agent_id: UUID) -> Sequence[AgentCredentialRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_credentials "
                "WHERE agent_id = %(agent_id)s ORDER BY created_at DESC",
                {"agent_id": agent_id},
            )
            return [AgentCredentialRecord.from_row(row) for row in rows]

    async def list_active_with_cookies(self, agent_id: UUID) -> Sequence[AgentCredentialRecord]:
        """Active credentials that have stored cookies for browser injection."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_credentials "
                "WHERE agent_id = %(agent_id)s AND status = 'active' "
                "AND cookies_json IS NOT NULL",
                {"agent_id": agent_id},
            )
            return [AgentCredentialRecord.from_row(row) for row in rows]

    async def get_for_agent(self, credential_id: UUID, agent_id: UUID) -> AgentCredentialRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_credentials WHERE id = %(id)s AND agent_id = %(agent_id)s",
                {"id": credential_id, "agent_id": agent_id},
            )
            return AgentCredentialRecord.from_row(row) if row else None


agent_credential_dao = AgentCredentialDAO()
