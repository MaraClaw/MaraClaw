"""Department persistence helpers for organization sync adapters."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import ClassVar, TypedDict

from app.core.json_types import (
    JsonObject,
    int_from_row,
    mapping_from_row,
    str_from_row,
    uuid_from_row,
    uuid_from_row_opt,
)
from app.dao import identity_provider_dao
from app.dao.org_department_dao import org_department_dao
from app.db.session import connection_ctx
from app.records.identity import IdentityProviderRecord
from app.services.org_sync.paths import build_department_path_map
from app.services.org_sync.types import ExternalDepartment
from app.services.org_sync.utils import _utcnow


class DepartmentCountNode(TypedDict):
    parent_id: uuid.UUID | None
    direct: int
    total: int
    children: list[uuid.UUID]


class DepartmentCountUpdate(TypedDict):
    id: uuid.UUID
    member_count: int


class OrgSyncDepartmentMixin:
    """Shared department persistence behavior for organization sync adapters."""

    config: JsonObject
    provider: IdentityProviderRecord | None
    provider_type: ClassVar[str]
    tenant_id: uuid.UUID | None

    async def _reconcile(self, db: object | None, provider_id: uuid.UUID, sync_start: datetime):
        """Mark records that were not updated in this sync as deleted."""
        del db
        now = _utcnow()
        async with connection_ctx() as conn:
            await conn.execute(
                "UPDATE org_members SET status = 'deleted', synced_at = %(now)s "
                + "WHERE provider_id = %(provider_id)s "
                + "AND synced_at < %(sync_start)s AND status <> 'deleted'",
                {"provider_id": provider_id, "sync_start": sync_start, "now": now},
            )
            await conn.execute(
                "UPDATE org_departments SET status = 'deleted', synced_at = %(now)s "
                + "WHERE provider_id = %(provider_id)s "
                + "AND synced_at < %(sync_start)s AND status <> 'deleted'",
                {"provider_id": provider_id, "sync_start": sync_start, "now": now},
            )

    async def _update_member_counts(self, db: object | None, provider_id: uuid.UUID):
        """Update member_count for all departments to include recursive sub-department members."""
        del db
        async with connection_ctx() as conn:
            # Direct counts
            await conn.execute(
                """
                UPDATE org_departments d
                SET member_count = (
                    SELECT COUNT(*) FROM org_members m
                    WHERE m.department_id = d.id AND m.status = 'active'
                )
                WHERE d.provider_id = %(provider_id)s AND d.status = 'active'
                """,
                {"provider_id": provider_id},
            )
            rows = await conn.fetchall(
                "SELECT id, parent_id, member_count FROM org_departments "
                + "WHERE provider_id = %(provider_id)s AND status = 'active'",
                {"provider_id": provider_id},
            )

        dept_map: dict[uuid.UUID, DepartmentCountNode] = {}
        for raw_row in rows:
            row = mapping_from_row(raw_row)
            dept_map[uuid_from_row(row.get("id"))] = {
                "parent_id": uuid_from_row_opt(row.get("parent_id")),
                "direct": int_from_row(row.get("member_count")),
                "total": 0,
                "children": [],
            }
        root_ids: list[uuid.UUID] = []
        for d_id, d_data in dept_map.items():
            parent_id = d_data["parent_id"]
            if parent_id and parent_id in dept_map:
                dept_map[parent_id]["children"].append(d_id)
            else:
                root_ids.append(d_id)

        def compute_total(node_id: uuid.UUID) -> int:
            node = dept_map[node_id]
            total = node["direct"]
            for child_id in node["children"]:
                total += compute_total(child_id)
            node["total"] = total
            return total

        for root_id in root_ids:
            _ = compute_total(root_id)

        for d_id, d_data in dept_map.items():
            await org_department_dao.set_member_count(d_id, d_data["total"])

    async def _ensure_provider(self, db: object | None) -> IdentityProviderRecord:
        """Ensure IdentityProvider record exists."""
        del db
        if self.provider:
            return self.provider

        provider_id = getattr(self, "provider_id", None)
        if isinstance(provider_id, uuid.UUID):
            self.provider = await identity_provider_dao.get(provider_id)
            if self.provider:
                return self.provider

        # Fallback by type (scoped by tenant)
        async with connection_ctx() as conn:
            if self.tenant_id:
                row = await conn.fetchone(
                    "SELECT * FROM identity_providers "
                    + "WHERE provider_type = %(ptype)s AND tenant_id = %(tenant_id)s LIMIT 1",
                    {"ptype": self.provider_type, "tenant_id": self.tenant_id},
                )
            else:
                row = await conn.fetchone(
                    "SELECT * FROM identity_providers WHERE provider_type = %(ptype)s AND tenant_id IS NULL LIMIT 1",
                    {"ptype": self.provider_type},
                )
            if row:
                self.provider = IdentityProviderRecord.from_row(mapping_from_row(row))
                return self.provider

            provider = await identity_provider_dao.create(
                obj_in=mapping_from_row(
                    {
                        "provider_type": self.provider_type,
                        "name": self.provider_type.capitalize(),
                        "is_active": True,
                        "config": self.config,
                        "tenant_id": self.tenant_id,
                    }
                )
            )
            self.provider = provider
            return provider

    async def _upsert_department(self, db: object | None, provider: IdentityProviderRecord, dept: ExternalDepartment):
        """Insert or update a department."""
        del db
        existing = await org_department_dao.get_by_external(
            external_id=dept.external_id,
            provider_id=provider.id,
        )

        now = _utcnow()
        path = dept.name
        parent_id = None
        if dept.parent_external_id:
            parent_dept = await org_department_dao.get_by_external(
                external_id=dept.parent_external_id,
                provider_id=provider.id,
            )
            if parent_dept:
                parent_id = parent_dept.id

        if existing:
            _ = await org_department_dao.update(
                db_obj=existing,
                obj_in=mapping_from_row(
                    {
                        "name": dept.name,
                        "member_count": dept.member_count,
                        "path": path,
                        "external_id": dept.external_id,
                        "provider_id": provider.id,
                        "parent_id": parent_id,
                        "status": "active",
                        "synced_at": now,
                    }
                ),
            )
        else:
            _ = await org_department_dao.create(
                obj_in=mapping_from_row(
                    {
                        "external_id": dept.external_id,
                        "provider_id": provider.id,
                        "name": dept.name,
                        "parent_id": parent_id,
                        "path": path,
                        "member_count": dept.member_count,
                        "tenant_id": self.tenant_id,
                        "status": "active",
                        "synced_at": now,
                    }
                )
            )

    async def _rebuild_department_paths(self, db: object | None, provider_id: uuid.UUID) -> dict[uuid.UUID, str]:
        """Normalize OrgDepartment.path using parent_id/name reverse derivation."""
        del db
        departments = list(await org_department_dao.list_for_provider(provider_id))
        path_map = build_department_path_map(departments)

        for dept in departments:
            new_path = path_map.get(dept.id, (dept.name or "").strip())
            if dept.path != new_path:
                await org_department_dao.set_path(dept.id, new_path)

        return path_map

    async def _refresh_member_department_paths(self, db: object | None, provider_id: uuid.UUID):
        """Refresh OrgMember.department_path from the normalized department tree."""
        del db
        departments = list(await org_department_dao.list_for_provider(provider_id))
        dept_path_map = build_department_path_map(departments)

        async with connection_ctx() as conn:
            members = await conn.fetchall(
                "SELECT id, department_id, department_path FROM org_members WHERE provider_id = %(provider_id)s",
                {"provider_id": provider_id},
            )
            for raw_member in members:
                member = mapping_from_row(raw_member)
                department_id = uuid_from_row_opt(member.get("department_id"))
                current_path = str_from_row(member.get("department_path"))
                new_path = dept_path_map.get(department_id, current_path) if department_id else current_path
                if new_path != current_path:
                    await conn.execute(
                        "UPDATE org_members SET department_path = %(path)s WHERE id = %(id)s",
                        {"path": new_path, "id": uuid_from_row(member.get("id"))},
                    )
