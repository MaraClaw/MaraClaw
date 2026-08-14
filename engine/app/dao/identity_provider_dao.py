"""DAO for identity_providers table (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, final
from uuid import UUID

from app.core.json_types import int_from_row
from app.dao.base import BaseDAO
from app.records.identity import IdentityProviderRecord

_COLUMNS = (
    "id",
    "provider_type",
    "name",
    "is_active",
    "sso_login_enabled",
    "config",
    "tenant_id",
    "created_at",
    "updated_at",
)


@final
class IdentityProviderDAO(BaseDAO[IdentityProviderRecord]):
    """DAO for IdentityProvider records."""

    table: ClassVar[str] = "identity_providers"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(IdentityProviderRecord.from_row)

    async def get_by_type_and_tenant(
        self,
        provider_type: str,
        tenant_id: UUID | None,
    ) -> IdentityProviderRecord | None:
        async with self.session() as db:
            if tenant_id is None:
                row = await db.fetchone(
                    f"SELECT {self._select_list()} FROM identity_providers "
                    + "WHERE provider_type = %(provider_type)s AND tenant_id IS NULL "
                    + "ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC "
                    + "LIMIT 1",
                    {"provider_type": provider_type},
                )
            else:
                row = await db.fetchone(
                    f"SELECT {self._select_list()} FROM identity_providers "
                    + "WHERE provider_type = %(provider_type)s AND tenant_id = %(tenant_id)s "
                    + "ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC "
                    + "LIMIT 1",
                    {"provider_type": provider_type, "tenant_id": tenant_id},
                )
            return IdentityProviderRecord.from_row(row) if row else None

    async def get_preferred(
        self,
        provider_type: str,
        tenant_id: UUID | None = None,
        *,
        is_active: bool | None = True,
    ) -> IdentityProviderRecord | None:
        """Preferred provider with optional active filter and global fallback."""
        provider = await self._get_preferred_scoped(provider_type, tenant_id, is_active=is_active)
        if provider is None and tenant_id is not None:
            provider = await self._get_preferred_scoped(provider_type, None, is_active=is_active)
        return provider

    async def _get_preferred_scoped(
        self,
        provider_type: str,
        tenant_id: UUID | None,
        *,
        is_active: bool | None,
    ) -> IdentityProviderRecord | None:
        params: dict[str, Any] = {"provider_type": provider_type}
        clauses = ["provider_type = %(provider_type)s"]
        if tenant_id is None:
            clauses.append("tenant_id IS NULL")
        else:
            clauses.append("tenant_id = %(tenant_id)s")
            params["tenant_id"] = tenant_id
        if is_active is not None:
            clauses.append("is_active IS TRUE" if is_active else "is_active IS FALSE")
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM identity_providers "
                + f"WHERE {' AND '.join(clauses)} "
                + "ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC "
                + "LIMIT 1",
                params,
            )
            return IdentityProviderRecord.from_row(row) if row else None

    async def list_active_sso_for_tenant(self, tenant_id: UUID | None) -> Sequence[IdentityProviderRecord]:
        """Active providers with SSO login enabled, scoped to tenant or global."""
        params: dict[str, Any] = {}
        if tenant_id is not None:
            tenant_sql = " AND tenant_id = %(tenant_id)s"
            params["tenant_id"] = tenant_id
        else:
            tenant_sql = " AND tenant_id IS NULL"
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM identity_providers "
                + f"WHERE is_active IS TRUE AND sso_login_enabled IS TRUE{tenant_sql}",
                params,
            )
            return [IdentityProviderRecord.from_row(row) for row in rows]

    async def list_active(self, tenant_id: UUID | None = None) -> Sequence[IdentityProviderRecord]:
        async with self.session() as db:
            if tenant_id is None:
                rows = await db.fetchall(
                    f"SELECT {self._select_list()} FROM identity_providers "
                    + "WHERE is_active IS TRUE AND tenant_id IS NULL "
                    + "ORDER BY name"
                )
            else:
                rows = await db.fetchall(
                    f"SELECT {self._select_list()} FROM identity_providers "
                    + "WHERE is_active IS TRUE AND tenant_id = %(tenant_id)s "
                    + "ORDER BY name",
                    {"tenant_id": tenant_id},
                )
            return [IdentityProviderRecord.from_row(row) for row in rows]

    async def list_active_by_type(self, provider_type: str) -> Sequence[IdentityProviderRecord]:
        """All active providers of a type across tenants (domain verification, etc.)."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM identity_providers "
                + "WHERE provider_type = %(provider_type)s AND is_active IS TRUE",
                {"provider_type": provider_type},
            )
            return [IdentityProviderRecord.from_row(row) for row in rows]

    async def list_active_sso_excluding_tenant(self, tenant_id: UUID) -> Sequence[IdentityProviderRecord]:
        """Active SSO-enabled providers belonging to other tenants (IP conflict checks)."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM identity_providers "
                + "WHERE sso_login_enabled IS TRUE AND is_active IS TRUE "
                + "AND tenant_id IS DISTINCT FROM %(tenant_id)s",
                {"tenant_id": tenant_id},
            )
            return [IdentityProviderRecord.from_row(row) for row in rows]

    async def get_or_create(
        self,
        provider_type: str,
        tenant_id: UUID | None,
        *,
        name: str | None = None,
        sso_login_enabled: bool = False,
    ) -> IdentityProviderRecord:
        provider = await self.get_by_type_and_tenant(provider_type, tenant_id)
        if provider:
            return provider
        return await self.create(
            obj_in={
                "provider_type": provider_type,
                "name": name or provider_type.capitalize(),
                "is_active": True,
                "sso_login_enabled": sso_login_enabled,
                "config": {},
                "tenant_id": tenant_id,
            }
        )

    async def list_for_tenant(self, tenant_id: UUID) -> Sequence[IdentityProviderRecord]:
        """All providers for a tenant (including inactive), newest first."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM identity_providers "
                + "WHERE tenant_id = %(tenant_id)s ORDER BY created_at DESC NULLS LAST",
                {"tenant_id": tenant_id},
            )
            return [IdentityProviderRecord.from_row(row) for row in rows]

    async def list_global(self) -> Sequence[IdentityProviderRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM identity_providers "
                + "WHERE tenant_id IS NULL ORDER BY created_at DESC NULLS LAST"
            )
            return [IdentityProviderRecord.from_row(row) for row in rows]

    async def count_active_sso(self, tenant_id: UUID) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT COUNT(*) FROM identity_providers "
                + "WHERE tenant_id = %(tenant_id)s AND sso_login_enabled IS TRUE AND is_active IS TRUE",
                {"tenant_id": tenant_id},
            )
            return int_from_row(value)

    async def delete_nullifying_org_refs(self, provider_id: UUID) -> None:
        """Nullify org member/department FKs then delete the provider."""
        async with self.session() as db:
            await db.execute(
                "UPDATE org_members SET provider_id = NULL WHERE provider_id = %(provider_id)s",
                {"provider_id": provider_id},
            )
            await db.execute(
                "UPDATE org_departments SET provider_id = NULL WHERE provider_id = %(provider_id)s",
                {"provider_id": provider_id},
            )
            await db.execute(
                "DELETE FROM identity_providers WHERE id = %(provider_id)s",
                {"provider_id": provider_id},
            )


identity_provider_dao = IdentityProviderDAO()
