"""DAO for org_members table (psycopg)."""

from __future__ import annotations

from uuid import UUID

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from app.dao.base import BaseDAO
from app.records.org import OrgMemberRecord

_COLUMNS = (
    "id",
    "open_id",
    "unionid",
    "external_id",
    "provider_id",
    "name",
    "name_translit_full",
    "name_translit_initial",
    "email",
    "avatar_url",
    "title",
    "department_id",
    "department_path",
    "phone",
    "status",
    "tenant_id",
    "user_id",
    "synced_at",
)


class OrgMemberDAO(BaseDAO[OrgMemberRecord]):
    """DAO for OrgMember records."""

    table: ClassVar[str] = "org_members"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory: Any = staticmethod(OrgMemberRecord.from_row)

    async def find_unbound_by_email(self, email: str, tenant_id: UUID) -> OrgMemberRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM org_members "
                + "WHERE email = %(email)s AND tenant_id = %(tenant_id)s AND user_id IS NULL LIMIT 1",
                {"email": email, "tenant_id": tenant_id},
            )
            return OrgMemberRecord.from_row(row) if row else None

    async def find_unbound_by_phone(self, phone: str, tenant_id: UUID) -> OrgMemberRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM org_members "
                + "WHERE phone = %(phone)s AND tenant_id = %(tenant_id)s AND user_id IS NULL LIMIT 1",
                {"phone": phone, "tenant_id": tenant_id},
            )
            return OrgMemberRecord.from_row(row) if row else None

    async def get_by_user_and_provider(
        self,
        user_id: UUID,
        tenant_id: UUID,
        provider_id: UUID,
    ) -> OrgMemberRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM org_members "
                + "WHERE user_id = %(user_id)s AND tenant_id = %(tenant_id)s "
                + "AND provider_id = %(provider_id)s LIMIT 1",
                {"user_id": user_id, "tenant_id": tenant_id, "provider_id": provider_id},
            )
            return OrgMemberRecord.from_row(row) if row else None

    async def find_unbound_by_email_and_provider(
        self,
        email: str,
        tenant_id: UUID,
        provider_id: UUID,
    ) -> OrgMemberRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM org_members "
                + "WHERE email = %(email)s AND tenant_id = %(tenant_id)s "
                + "AND provider_id = %(provider_id)s AND user_id IS NULL LIMIT 1",
                {"email": email, "tenant_id": tenant_id, "provider_id": provider_id},
            )
            return OrgMemberRecord.from_row(row) if row else None

    async def find_unbound_by_phone_and_provider(
        self,
        phone: str,
        tenant_id: UUID,
        provider_id: UUID,
    ) -> OrgMemberRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM org_members "
                + "WHERE phone = %(phone)s AND tenant_id = %(tenant_id)s "
                + "AND provider_id = %(provider_id)s AND user_id IS NULL LIMIT 1",
                {"phone": phone, "tenant_id": tenant_id, "provider_id": provider_id},
            )
            return OrgMemberRecord.from_row(row) if row else None

    async def get_by_user_and_tenant_and_provider(
        self,
        user_id: UUID,
        tenant_id: UUID,
        provider_id: UUID,
    ) -> Sequence[OrgMemberRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM org_members "
                + "WHERE user_id = %(user_id)s AND tenant_id = %(tenant_id)s "
                + "AND provider_id = %(provider_id)s",
                {"user_id": user_id, "tenant_id": tenant_id, "provider_id": provider_id},
            )
            return [OrgMemberRecord.from_row(row) for row in rows]

    async def update_fields(self, member_id: UUID, fields: Mapping[str, Any]) -> OrgMemberRecord | None:
        """Update selected columns for one org member."""
        member = await self.get(member_id)
        if member is None:
            return None
        return await self.update(db_obj=member, obj_in=fields)

    async def find_active_by_provider_field(
        self,
        provider_id: UUID,
        field: str,
        value: str,
    ) -> OrgMemberRecord | None:
        """Find an active org member by provider and one identity field."""
        if field not in {"unionid", "open_id", "external_id"}:
            raise ValueError(f"unsupported identity field: {field}")
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM org_members "
                + f"WHERE provider_id = %(provider_id)s AND status = 'active' AND {field} = %(value)s "
                + "LIMIT 1",
                {"provider_id": provider_id, "value": value},
            )
            return OrgMemberRecord.from_row(row) if row else None

    async def list_by_user_and_provider(
        self,
        user_id: UUID,
        provider_id: UUID,
    ) -> Sequence[OrgMemberRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM org_members "
                + "WHERE user_id = %(user_id)s AND provider_id = %(provider_id)s",
                {"user_id": user_id, "provider_id": provider_id},
            )
            return [OrgMemberRecord.from_row(row) for row in rows]

    async def get_by_unionid(self, provider_id: UUID, unionid: str) -> OrgMemberRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM org_members "
                + "WHERE provider_id = %(provider_id)s AND unionid = %(unionid)s LIMIT 1",
                {"provider_id": provider_id, "unionid": unionid},
            )
            return OrgMemberRecord.from_row(row) if row else None

    async def find_by_external_or_open_id(
        self,
        *,
        provider_id: UUID,
        external_id: str | None,
        open_id: str | None,
        require_unionid_compatible: bool = False,
        unionid: str | None = None,
    ) -> OrgMemberRecord | None:
        """Fallback lookup when unionid is missing or shell records need attachment."""
        if not external_id and not open_id:
            return None
        params: dict[str, Any] = {"provider_id": provider_id}
        or_parts: list[str] = []
        if external_id:
            or_parts.append("external_id = %(external_id)s")
            params["external_id"] = external_id
        if open_id:
            or_parts.append("open_id = %(open_id)s")
            params["open_id"] = open_id
        unionid_sql = ""
        if require_unionid_compatible and unionid:
            unionid_sql = " AND (unionid IS NULL OR unionid = '' OR unionid = %(unionid)s)"
            params["unionid"] = unionid
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM org_members "
                + f"WHERE provider_id = %(provider_id)s AND ({' OR '.join(or_parts)})"
                + f"{unionid_sql} LIMIT 1",
                params,
            )
            return OrgMemberRecord.from_row(row) if row else None

    async def get_by_provider_and_open_id(self, provider_id: UUID, open_id: str) -> OrgMemberRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM org_members "
                + "WHERE provider_id = %(provider_id)s AND open_id = %(open_id)s LIMIT 1",
                {"provider_id": provider_id, "open_id": open_id},
            )
            return OrgMemberRecord.from_row(row) if row else None

    async def get_by_provider_and_external_id(self, provider_id: UUID, external_id: str) -> OrgMemberRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM org_members "
                + "WHERE provider_id = %(provider_id)s AND external_id = %(external_id)s LIMIT 1",
                {"provider_id": provider_id, "external_id": external_id},
            )
            return OrgMemberRecord.from_row(row) if row else None

    async def find_active_by_any_ids(
        self,
        *,
        provider_id: UUID,
        unionid: str | None = None,
        open_id: str | None = None,
        external_id: str | None = None,
    ) -> OrgMemberRecord | None:
        """Find active member matching any of the provided identity fields.

        Prefers rows already linked to a platform user, then oldest synced.
        """
        or_parts: list[str] = []
        params: dict[str, Any] = {"provider_id": provider_id}
        if unionid:
            or_parts.append("unionid = %(unionid)s")
            params["unionid"] = unionid
        if open_id:
            or_parts.append("open_id = %(open_id)s")
            params["open_id"] = open_id
        if external_id:
            or_parts.append("external_id = %(external_id)s")
            params["external_id"] = external_id
        if not or_parts:
            return None
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM org_members "
                + "WHERE provider_id = %(provider_id)s AND status = 'active' "
                + f"AND ({' OR '.join(or_parts)}) "
                + "ORDER BY (user_id IS NOT NULL) DESC, synced_at ASC NULLS LAST "
                + "LIMIT 1",
                params,
            )
            return OrgMemberRecord.from_row(row) if row else None

    async def get_active_for_user_and_provider(
        self,
        *,
        user_id: UUID,
        provider_id: UUID,
        tenant_id: UUID | None = None,
    ) -> OrgMemberRecord | None:
        params: dict[str, Any] = {"user_id": user_id, "provider_id": provider_id}
        tenant_sql = ""
        if tenant_id is not None:
            tenant_sql = " AND tenant_id = %(tenant_id)s"
            params["tenant_id"] = tenant_id
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM org_members "
                + "WHERE user_id = %(user_id)s AND provider_id = %(provider_id)s "
                + f"AND status = 'active'{tenant_sql} LIMIT 1",
                params,
            )
            return OrgMemberRecord.from_row(row) if row else None

    async def find_related_to_agent_by_name(self, agent_id: UUID, name: str) -> OrgMemberRecord | None:
        """Find an org member linked to an agent via agent_relationships by display name."""
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list('m')} FROM org_members m "
                + "JOIN agent_relationships r ON r.member_id = m.id "
                + "WHERE r.agent_id = %(agent_id)s AND m.name = %(name)s LIMIT 1",
                {"agent_id": agent_id, "name": name},
            )
            return OrgMemberRecord.from_row(row) if row else None

    async def list_active_with_user_for_tenant(self, tenant_id: UUID) -> Sequence[OrgMemberRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM org_members "
                + "WHERE tenant_id = %(tenant_id)s AND status = 'active' AND user_id IS NOT NULL",
                {"tenant_id": tenant_id},
            )
            return [OrgMemberRecord.from_row(row) for row in rows]

    async def list_active_ids_for_tenant(self, tenant_id: UUID) -> Sequence[UUID]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id FROM org_members WHERE tenant_id = %(tenant_id)s AND status = 'active'",
                {"tenant_id": tenant_id},
            )
            return [row["id"] for row in rows]

    async def names_for_ids(self, member_ids: Sequence[UUID]) -> dict[UUID, str]:
        if not member_ids:
            return {}
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id, name FROM org_members WHERE id = ANY(%(ids)s)",
                {"ids": list(member_ids)},
            )
            return {row["id"]: (row.get("name") or "") for row in rows}

    async def get_user_id(self, member_id: UUID) -> UUID | None:
        async with self.session() as db:
            return await db.fetchval(
                "SELECT user_id FROM org_members WHERE id = %(id)s",
                {"id": member_id},
            )

    async def unbind_user_from_tenant(self, user_id: UUID, tenant_id: UUID) -> None:
        """Drop directory links when a member leaves this organization."""
        async with self.session() as db:
            await db.execute(
                "UPDATE org_members SET user_id = NULL "
                + "WHERE user_id = %(user_id)s AND tenant_id = %(tenant_id)s",
                {"user_id": user_id, "tenant_id": tenant_id},
            )

    async def get_active_by_user_and_tenant(self, user_id: UUID, tenant_id: UUID) -> OrgMemberRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM org_members "
                + "WHERE user_id = %(user_id)s AND tenant_id = %(tenant_id)s AND status = 'active' LIMIT 1",
                {"user_id": user_id, "tenant_id": tenant_id},
            )
            return OrgMemberRecord.from_row(row) if row else None

    async def count_active(
        self,
        *,
        tenant_id: UUID | None = None,
        provider_id: UUID | None = None,
    ) -> int:
        params: dict[str, Any] = {}
        clauses = ["status = 'active'"]
        if tenant_id is not None:
            clauses.append("tenant_id = %(tenant_id)s")
            params["tenant_id"] = tenant_id
        if provider_id is not None:
            clauses.append("provider_id = %(provider_id)s")
            params["provider_id"] = provider_id
        where = " AND ".join(clauses)
        async with self.session() as db:
            value = await db.fetchval(
                f"SELECT COUNT(*) FROM org_members WHERE {where}",
                params or None,
            )
            return int(value or 0)

    async def list_active_filtered(
        self,
        *,
        tenant_id: UUID | None = None,
        provider_id: UUID | None = None,
        department_ids: Sequence[UUID] | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> list[tuple[OrgMemberRecord, str | None, str | None]]:
        """Active members with provider join for enterprise org member list."""
        params: dict[str, Any] = {"limit": limit}
        clauses = ["m.status = 'active'"]
        if tenant_id is not None:
            clauses.append("m.tenant_id = %(tenant_id)s")
            params["tenant_id"] = tenant_id
        if provider_id is not None:
            clauses.append("m.provider_id = %(provider_id)s")
            params["provider_id"] = provider_id
        if department_ids is not None:
            clauses.append("m.department_id = ANY(%(department_ids)s)")
            params["department_ids"] = list(department_ids)
        if search:
            params["search"] = f"%{search}%"
            clauses.append(
                "(m.name ILIKE %(search)s OR m.name_translit_full ILIKE %(search)s "
                + "OR m.name_translit_initial ILIKE %(search)s)"
            )
        where = " AND ".join(clauses)
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list('m')}, "
                + "p.name AS provider_name, p.provider_type AS provider_type "
                + "FROM org_members m "
                + "LEFT JOIN identity_providers p ON p.id = m.provider_id "
                + f"WHERE {where} ORDER BY m.name LIMIT %(limit)s",
                params,
            )
            return [
                (
                    OrgMemberRecord.from_row(row),
                    row.get("provider_name"),
                    row.get("provider_type"),
                )
                for row in rows
            ]

    async def list_permission_candidates(
        self,
        *,
        tenant_id: UUID,
        search: str | None = None,
        limit: int = 50,
    ) -> Sequence[OrgMemberRecord]:
        """Active org members for agent permission candidate picker."""
        params: dict[str, Any] = {"tenant_id": tenant_id, "limit": limit}
        search_sql = ""
        if search:
            params["search"] = f"%{search}%"
            search_sql = (
                " AND ("
                + "name ILIKE %(search)s OR email ILIKE %(search)s "
                + "OR name_translit_full ILIKE %(search)s OR name_translit_initial ILIKE %(search)s"
                + ")"
            )
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM org_members "
                + f"WHERE tenant_id = %(tenant_id)s AND status = 'active'{search_sql} "
                + "ORDER BY name ASC LIMIT %(limit)s",
                params,
            )
            return [OrgMemberRecord.from_row(row) for row in rows]

    async def list_relationship_candidates(
        self,
        *,
        tenant_id: UUID,
        search: str | None = None,
        allowed_user_ids: Sequence[UUID] | None = None,
        limit: int = 200,
    ) -> list[tuple[OrgMemberRecord, str | None, str | None, UUID | None]]:
        """Active org members eligible for agent human relationships.

        Returns (member, provider_name, provider_type, linked_user_id) tuples.
        linked_user_id is only set when the platform user is active in the same tenant.
        """
        params: dict[str, Any] = {"tenant_id": tenant_id, "limit": limit}
        search_sql = ""
        if search:
            params["search"] = f"%{search}%"
            search_sql = (
                " AND ("
                + "m.name ILIKE %(search)s OR m.name_translit_full ILIKE %(search)s "
                + "OR m.name_translit_initial ILIKE %(search)s OR m.email ILIKE %(search)s"
                + ")"
            )
        allowed_sql = ""
        if allowed_user_ids is not None:
            params["allowed_user_ids"] = list(allowed_user_ids)
            allowed_sql = " AND (m.user_id IS NULL OR lu.id = ANY(%(allowed_user_ids)s))"
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list('m')}, "
                + "p.name AS provider_name, p.provider_type AS provider_type, "
                + "lu.id AS linked_user_id "
                + "FROM org_members m "
                + "LEFT JOIN identity_providers p ON p.id = m.provider_id "
                + "LEFT JOIN users lu ON lu.id = m.user_id "
                + "AND lu.tenant_id = %(tenant_id)s AND lu.is_active IS TRUE "
                + "WHERE m.tenant_id = %(tenant_id)s AND m.status = 'active' "
                + "AND (m.user_id IS NULL OR lu.id IS NOT NULL)"
                + f"{search_sql}{allowed_sql} "
                + "ORDER BY m.name LIMIT %(limit)s",
                params,
            )
            return [
                (
                    OrgMemberRecord.from_row(row),
                    row.get("provider_name"),
                    row.get("provider_type"),
                    row.get("linked_user_id"),
                )
                for row in rows
            ]


org_member_dao = OrgMemberDAO()
