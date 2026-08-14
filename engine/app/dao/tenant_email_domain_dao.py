"""DAO for tenant_email_domains."""

from __future__ import annotations

from uuid import UUID

from collections.abc import Sequence
from typing import Any, ClassVar

from app.dao.base import BaseDAO
from app.records.tenant_email_domain import TenantEmailDomainRecord

_COLUMNS = (
    "id",
    "tenant_id",
    "domain",
    "is_default",
    "created_at",
)


class TenantEmailDomainDAO(BaseDAO[TenantEmailDomainRecord]):
    """CRUD plus lookup by domain / tenant."""

    table: ClassVar[str] = "tenant_email_domains"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory: Any = staticmethod(TenantEmailDomainRecord.from_row)

    async def get_by_domain(self, domain: str) -> TenantEmailDomainRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM tenant_email_domains WHERE domain = %(domain)s LIMIT 1",
                {"domain": domain},
            )
            return TenantEmailDomainRecord.from_row(row) if row else None

    async def list_for_tenant(self, tenant_id: UUID) -> Sequence[TenantEmailDomainRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tenant_email_domains "
                + "WHERE tenant_id = %(tenant_id)s ORDER BY is_default DESC, domain ASC",
                {"tenant_id": tenant_id},
            )
            return [TenantEmailDomainRecord.from_row(row) for row in rows]

    async def get_default_for_tenant(self, tenant_id: UUID) -> TenantEmailDomainRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM tenant_email_domains "
                + "WHERE tenant_id = %(tenant_id)s AND is_default IS TRUE LIMIT 1",
                {"tenant_id": tenant_id},
            )
            return TenantEmailDomainRecord.from_row(row) if row else None

    async def clear_default_for_tenant(self, tenant_id: UUID) -> None:
        async with self.session() as db:
            await db.execute(
                "UPDATE tenant_email_domains SET is_default = FALSE "
                + "WHERE tenant_id = %(tenant_id)s AND is_default IS TRUE",
                {"tenant_id": tenant_id},
            )


tenant_email_domain_dao = TenantEmailDomainDAO()
