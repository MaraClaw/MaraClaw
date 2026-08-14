"""DAO for org_departments (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar, final
from uuid import UUID

from app.core.json_types import uuid_list_from_rows
from app.dao.base import BaseDAO
from app.records.org_department import OrgDepartmentRecord

_COLUMNS = (
    "id",
    "external_id",
    "provider_id",
    "name",
    "parent_id",
    "path",
    "member_count",
    "status",
    "tenant_id",
    "synced_at",
)


@final
class OrgDepartmentDAO(BaseDAO[OrgDepartmentRecord]):
    """DAO for organization department rows."""

    table: ClassVar[str] = "org_departments"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(OrgDepartmentRecord.from_row)

    async def get_by_external(self, *, external_id: str, provider_id: UUID) -> OrgDepartmentRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM org_departments "
                + "WHERE external_id = %(external_id)s AND provider_id = %(provider_id)s "
                + "LIMIT 1",
                {"external_id": external_id, "provider_id": provider_id},
            )
            return OrgDepartmentRecord.from_row(row) if row else None

    async def list_for_provider(self, provider_id: UUID) -> Sequence[OrgDepartmentRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM org_departments WHERE provider_id = %(provider_id)s",
                {"provider_id": provider_id},
            )
            return [OrgDepartmentRecord.from_row(row) for row in rows]

    async def list_active_counts(self, provider_id: UUID) -> Sequence[dict[str, Any]]:
        async with self.session() as db:
            return await db.fetchall(
                "SELECT id, parent_id, member_count FROM org_departments "
                + "WHERE provider_id = %(provider_id)s AND status = 'active'",
                {"provider_id": provider_id},
            )

    async def mark_stale_deleted(self, *, provider_id: UUID, sync_start: datetime, now: datetime) -> None:
        async with self.session() as db:
            await db.execute(
                "UPDATE org_departments SET status = 'deleted', synced_at = %(now)s "
                + "WHERE provider_id = %(provider_id)s "
                + "AND synced_at < %(sync_start)s AND status <> 'deleted'",
                {"provider_id": provider_id, "sync_start": sync_start, "now": now},
            )

    async def set_member_count(self, department_id: UUID, member_count: int) -> None:
        async with self.session() as db:
            await db.execute(
                "UPDATE org_departments SET member_count = %(member_count)s WHERE id = %(id)s",
                {"id": department_id, "member_count": member_count},
            )

    async def set_path(self, department_id: UUID, path: str) -> None:
        async with self.session() as db:
            await db.execute(
                "UPDATE org_departments SET path = %(path)s WHERE id = %(id)s",
                {"id": department_id, "path": path},
            )

    async def list_active_with_provider(
        self,
        *,
        tenant_id: UUID | None = None,
        provider_id: UUID | None = None,
    ) -> Sequence[dict[str, Any]]:
        """Active departments joined with provider name/type for enterprise org UI."""
        params: dict[str, Any] = {}
        clauses = ["d.status = 'active'"]
        if tenant_id is not None:
            clauses.append("d.tenant_id = %(tenant_id)s")
            params["tenant_id"] = tenant_id
        if provider_id is not None:
            clauses.append("d.provider_id = %(provider_id)s")
            params["provider_id"] = provider_id
        where = " AND ".join(clauses)
        async with self.session() as db:
            return await db.fetchall(
                f"SELECT {self._select_list('d')}, "
                + "p.name AS provider_name, p.provider_type AS provider_type "
                + "FROM org_departments d "
                + "LEFT JOIN identity_providers p ON p.id = d.provider_id "
                + f"WHERE {where} ORDER BY d.name",
                params or None,
            )

    async def subtree_ids(self, department_id: UUID) -> list[UUID]:
        """Return department id plus all descendant ids (path prefix)."""
        dept = await self.get(department_id)
        if not dept:
            return [department_id]
        if not dept.path:
            return [department_id]
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id FROM org_departments WHERE id = %(id)s OR path LIKE %(prefix)s",
                {"id": department_id, "prefix": f"{dept.path}/%"},
            )
            return uuid_list_from_rows(rows) or [department_id]


org_department_dao = OrgDepartmentDAO()
