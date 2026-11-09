"""DAO for agent_credentials"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import ClassVar, final, override
from uuid import UUID, uuid4

from typing_extensions import TypedDict

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row
from app.core.stored_credentials import StoredCredentialError, open_credential, seal_credential
from app.dao.base import BaseDAO
from app.dao.credential_contexts import cookie_context
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

    @override
    def _from_row(self, row: Mapping[str, object]) -> AgentCredentialRecord:
        record = self.record_factory(row)
        if record.cookies_json not in (None, ""):
            _ = open_credential(record.cookies_json, cookie_context(record.agent_id, record.id))
        return record

    @override
    def _deleted_row(self, row: Mapping[str, object]) -> AgentCredentialRecord:
        record = self.record_factory(row)
        return replace(record, cookies_json="stored" if record.cookies_json not in (None, "") else None)

    @override
    async def create(self, *, obj_in: Mapping[str, object]) -> AgentCredentialRecord:
        data = {**obj_in, "id": obj_in.get("id", uuid4())}
        value = str_from_row(data.get("cookies_json"))
        if value:
            data["cookies_json"] = seal_credential(
                value, cookie_context(uuid_from_row(data["agent_id"]), uuid_from_row(data["id"]))
            )
        return await super().create(obj_in=data)

    async def get_metadata_for_agent(self, credential_id: UUID, agent_id: UUID) -> AgentCredentialRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_credentials WHERE id = %(id)s AND agent_id = %(agent_id)s",
                {"id": credential_id, "agent_id": agent_id},
            )
            return self._deleted_row(row) if row is not None else None

    async def list_metadata_for_agent(self, agent_id: UUID) -> Sequence[AgentCredentialRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_credentials WHERE agent_id = %(agent_id)s "
                "ORDER BY created_at DESC",
                {"agent_id": agent_id},
            )
            return [self._deleted_row(row) for row in rows]

    @override
    async def update(self, *, db_obj: AgentCredentialRecord, obj_in: Mapping[str, object]) -> AgentCredentialRecord:
        changes: CredentialChanges = {}
        if "credential_type" in obj_in:
            changes["credential_type"] = str_from_row(obj_in["credential_type"])
        if "platform" in obj_in:
            changes["platform"] = str_from_row(obj_in["platform"])
        if "display_name" in obj_in:
            changes["display_name"] = (
                str_from_row(obj_in["display_name"]) if obj_in["display_name"] is not None else None
            )
        if "status" in obj_in:
            changes["status"] = str_from_row(obj_in["status"])
        if "cookies_json" in obj_in:
            changes["cookies_json"] = (
                str_from_row(obj_in["cookies_json"]) if obj_in["cookies_json"] is not None else None
            )
        if "cookies_updated_at" in obj_in:
            changes["cookies_updated_at"] = datetime_from_row(obj_in["cookies_updated_at"])
        if "last_login_at" in obj_in:
            changes["last_login_at"] = datetime_from_row(obj_in["last_login_at"])
        return await self.edit(CredentialRevision(db_obj.id, db_obj.agent_id, db_obj.revision), changes)

    async def edit(self, target: CredentialRevision, changes: CredentialChanges) -> AgentCredentialRecord:
        """Compare and advance the observed revision in the same row write."""
        data = dict(changes)
        if data.get("cookies_json"):
            data["cookies_json"] = seal_credential(
                str_from_row(data["cookies_json"]), cookie_context(target.agent_id, target.credential_id)
            )
        assignments = [f"{column} = %({column})s" for column in data]
        assignments.extend(("revision = revision + 1", "last_injected_at = NULL", "updated_at = NOW()"))
        async with self.session() as db:
            row = await db.fetchone(
                f"UPDATE agent_credentials SET {', '.join(assignments)} "
                "WHERE id = %(id)s AND agent_id = %(agent_id)s AND revision = %(expected_revision)s "
                f"RETURNING {self._select_list()}",
                {
                    **data,
                    "id": target.credential_id,
                    "agent_id": target.agent_id,
                    "expected_revision": target.revision,
                },
            )
            if row is None:
                raise CredentialRevisionConflictError
            try:
                return self._from_row(row)
            except StoredCredentialError:
                return self._deleted_row(row)

    async def publish_injection(self, target: CredentialRevision, injected_at: datetime) -> bool:
        """Publish evidence only while the injected snapshot is still current and active."""
        async with self.session() as db:
            current = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_credentials "
                "WHERE id = %(id)s AND agent_id = %(agent_id)s AND revision = %(revision)s "
                "AND status = 'active' AND cookies_json IS NOT NULL FOR UPDATE",
                {"id": target.credential_id, "agent_id": target.agent_id, "revision": target.revision},
            )
            if current is None:
                return False
            _ = self._from_row(current)
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
            return [self._from_row(row) for row in rows]

    async def list_active_with_cookies(self, agent_id: UUID) -> Sequence[AgentCredentialRecord]:
        """Active credentials that have stored cookies for browser injection."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_credentials "
                "WHERE agent_id = %(agent_id)s AND status = 'active' "
                "AND cookies_json IS NOT NULL",
                {"agent_id": agent_id},
            )
            return [self._from_row(row) for row in rows]

    async def get_for_agent(self, credential_id: UUID, agent_id: UUID) -> AgentCredentialRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_credentials WHERE id = %(id)s AND agent_id = %(agent_id)s",
                {"id": credential_id, "agent_id": agent_id},
            )
            return self._from_row(row) if row is not None else None


agent_credential_dao = AgentCredentialDAO()
