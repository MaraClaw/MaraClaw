"""DAO for agent_credentials"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import ClassVar, final, override
from uuid import UUID

from typing_extensions import TypedDict

from app.dao.base import BaseDAO
from app.records.agent_credential import AgentCredentialRecord, CredentialRevision

_COLUMNS = (
    "id",
    "agent_id",
    "revision",
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


class CredentialChanges(TypedDict, total=False):
    credential_type: str | None
    platform: str | None
    display_name: str | None
    status: str | None
    cookies_json: str | None
    cookies_updated_at: datetime | None
    last_login_at: datetime | None


class CredentialRevisionConflictError(Exception):
    @override
    def __str__(self) -> str:
        return "Credential changed; reload it before replacing it."


class CredentialConflictDetail(TypedDict):
    code: str
    message: str


def revision_conflict_detail() -> CredentialConflictDetail:
    return {"code": "credential_revision_conflict", "message": str(CredentialRevisionConflictError())}


@final
class AgentCredentialDAO(BaseDAO[AgentCredentialRecord]):
    table: ClassVar[str] = "agent_credentials"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(AgentCredentialRecord.from_row)

    async def edit(self, target: CredentialRevision, changes: CredentialChanges) -> AgentCredentialRecord:
        """Compare and advance the observed revision in the same row write."""
        assignments = [f"{column} = %({column})s" for column in changes]
        assignments.extend(("revision = revision + 1", "last_injected_at = NULL", "updated_at = NOW()"))
        async with self.session() as db:
            row = await db.fetchone(
                f"UPDATE agent_credentials SET {', '.join(assignments)} "
                "WHERE id = %(id)s AND agent_id = %(agent_id)s AND revision = %(expected_revision)s "
                f"RETURNING {self._select_list()}",
                {
                    **changes,
                    "id": target.credential_id,
                    "agent_id": target.agent_id,
                    "expected_revision": target.revision,
                },
            )
            if row is None:
                raise CredentialRevisionConflictError
            return AgentCredentialRecord.from_row(row)

    async def publish_injection(self, target: CredentialRevision, injected_at: datetime) -> bool:
        """Publish evidence only while the injected snapshot is still current and active."""
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE agent_credentials SET last_injected_at = %(injected_at)s "
                "WHERE id = %(id)s AND agent_id = %(agent_id)s AND revision = %(revision)s "
                "AND status = 'active' AND cookies_json IS NOT NULL RETURNING id",
                {
                    "id": target.credential_id,
                    "agent_id": target.agent_id,
                    "revision": target.revision,
                    "injected_at": injected_at,
                },
            )
            return row is not None

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
            return AgentCredentialRecord.from_row(row) if row is not None else None


agent_credential_dao = AgentCredentialDAO()
