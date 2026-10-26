"""DAO for tenants table"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, TypedDict, final, override
from uuid import UUID

from app.core.json_types import int_from_row, str_findall, uuid_from_row
from app.core.row_memo import memo_drop, memo_get, memo_set
from app.core.tenant_cache import bump_tenant_cache, get_cached_tenant, peek_tenant_version, set_cached_tenant
from app.dao.base import BaseDAO, as_uuid
from app.dao.deletion_cleanup import execute_cleanup
from app.dao.tenant_deletion_sql import TENANT_DELETE_CLEANUP_SQL
from app.records.tenant import TenantRecord

__all__ = ["TENANT_DELETE_CLEANUP_SQL", "TenantDAO", "TenantDeletionPreview", "tenant_dao", "tenant_name_tsquery"]

_SEARCH_TOKEN = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_MAX_SEARCH_TOKENS = 8
_DEFAULT_SEARCH_LIMIT = 50


class TenantDeletionPreview(TypedDict):
    users_count: int
    agents_count: int
    schedules_count: int


def tenant_name_tsquery(raw: str) -> str | None:
    """Build a prefix ``simple`` tsquery so ``mara`` matches ``MaraClaw``."""
    tokens = str_findall(_SEARCH_TOKEN, (raw or "").strip().lower())
    if not tokens:
        return None
    return " & ".join(f"{token}:*" for token in tokens[:_MAX_SEARCH_TOKENS])


_TENANT_COLUMNS = (
    "id",
    "name",
    "slug",
    "im_provider",
    "im_config",
    "is_active",
    "created_at",
    "default_message_limit",
    "default_message_period",
    "default_max_agents",
    "default_agent_ttl_hours",
    "default_max_llm_calls_per_day",
    "min_heartbeat_interval_minutes",
    "timezone",
    "country_region",
    "sso_enabled",
    "sso_domain",
    "default_max_triggers",
    "min_poll_interval_floor",
    "max_webhook_rate_ceiling",
    "a2a_async_enabled",
    "default_model_id",
    "default_fallback_model_id",
    "default_secondary_model_id",
    "is_system",
    "is_default_end_user_org",
)


@final
class TenantDAO(BaseDAO[TenantRecord]):
    """DAO for Tenant records."""

    table: ClassVar[str] = "tenants"
    columns: ClassVar[tuple[str, ...]] = _TENANT_COLUMNS
    record_factory = staticmethod(TenantRecord.from_row)

    @override
    async def get(self, record_id: UUID) -> TenantRecord | None:
        cached = memo_get("tenant", record_id)
        if isinstance(cached, TenantRecord):
            return cached
        try:
            tenant_id = as_uuid(record_id)
        except TypeError, ValueError:
            tenant_id = None
        observed_ver = None
        if tenant_id is not None:
            redis_hit = await get_cached_tenant(tenant_id)
            if redis_hit is not None:
                memo_set("tenant", redis_hit.id, redis_hit)
                return redis_hit
            observed_ver = await peek_tenant_version(tenant_id)
        tenant = await super().get(record_id)
        if tenant is not None:
            memo_set("tenant", tenant.id, tenant)
            await set_cached_tenant(tenant, observed_ver=observed_ver)
        return tenant

    @override
    async def update(self, *, db_obj: TenantRecord, obj_in: Mapping[str, Any]) -> TenantRecord:
        from app.core.acl_locks import invalidate_tenant_agents, lock_tenants
        from app.db.session import optional_connection_ctx

        async with optional_connection_ctx() as db:
            if db is not None:
                await lock_tenants(db, (db_obj.id,))
                if "is_active" in obj_in:
                    await invalidate_tenant_agents(db, db_obj.id)
            updated = await super().update(db_obj=db_obj, obj_in=obj_in)
            memo_set("tenant", updated.id, updated)
            await bump_tenant_cache(updated.id)
            return updated

    @override
    async def delete(self, *, record_id: UUID) -> TenantRecord | None:
        from app.core.acl_locks import invalidate_tenant_agents, lock_tenants
        from app.db.session import optional_connection_ctx

        async with optional_connection_ctx() as db:
            if db is not None:
                await lock_tenants(db, (record_id,))
                await invalidate_tenant_agents(db, record_id)
            deleted = await super().delete(record_id=record_id)
            if deleted is not None:
                memo_drop("tenant", deleted.id)
                await bump_tenant_cache(deleted.id)
            return deleted

    async def get_default_end_user_org(self) -> TenantRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM tenants "
                "WHERE is_default_end_user_org IS TRUE AND is_active IS TRUE LIMIT 1",
            )
            return TenantRecord.from_row(row) if row is not None else None

    async def get_by_slug(self, slug: str) -> TenantRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM tenants WHERE slug = %(slug)s LIMIT 1",
                {"slug": slug},
            )
            return TenantRecord.from_row(row) if row is not None else None

    async def get_first_by_created_at(self) -> TenantRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM tenants ORDER BY created_at ASC NULLS LAST LIMIT 1"
            )
            return TenantRecord.from_row(row) if row is not None else None

    async def get_by_ids(self, ids: Sequence[UUID]) -> Sequence[TenantRecord]:
        if not ids:
            empty: list[TenantRecord] = []
            return empty
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tenants WHERE id = ANY(%(ids)s)",
                {"ids": list(ids)},
            )
            return [TenantRecord.from_row(row) for row in rows]

    async def get_by_sso_domain(self, domain: str) -> TenantRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM tenants "
                "WHERE sso_domain = %(domain)s AND is_active IS TRUE LIMIT 1",
                {"domain": domain.lower()},
            )
            return TenantRecord.from_row(row) if row is not None else None

    async def find_by_sso_domain_ilike(self, domain: str) -> TenantRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM tenants WHERE sso_domain ILIKE %(pattern)s LIMIT 1",
                {"pattern": f"%{domain}%"},
            )
            return TenantRecord.from_row(row) if row is not None else None

    async def find_by_name_ilike(self, name_fragment: str) -> TenantRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM tenants WHERE name ILIKE %(pattern)s LIMIT 1",
                {"pattern": f"%{name_fragment}%"},
            )
            return TenantRecord.from_row(row) if row is not None else None

    async def list_ordered_by_created_at(self, *, desc: bool = True) -> Sequence[TenantRecord]:
        order = "DESC" if desc else "ASC"
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tenants ORDER BY created_at {order} NULLS LAST"
            )
            return [TenantRecord.from_row(row) for row in rows]

    async def search_by_name(self, raw: str, *, limit: int = _DEFAULT_SEARCH_LIMIT) -> Sequence[TenantRecord]:
        query = tenant_name_tsquery(raw)
        if query is None:
            return await self.list_ordered_by_created_at(desc=True)
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tenants "
                "WHERE name_tsv @@ to_tsquery('simple', %(query)s) "
                "ORDER BY ts_rank_cd(name_tsv, to_tsquery('simple', %(query)s)) DESC, "
                "created_at DESC NULLS LAST "
                "LIMIT %(limit)s",
                {"query": query, "limit": limit},
            )
            return [TenantRecord.from_row(row) for row in rows]

    async def get_by_sso_domain_exact(self, sso_domain: str) -> TenantRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM tenants WHERE sso_domain = %(domain)s LIMIT 1",
                {"domain": sso_domain},
            )
            return TenantRecord.from_row(row) if row is not None else None

    async def get_by_sso_domain_like(self, prefix: str) -> TenantRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM tenants WHERE sso_domain LIKE %(pattern)s LIMIT 1",
                {"pattern": f"{prefix}%"},
            )
            return TenantRecord.from_row(row) if row is not None else None

    async def count_created_before(self, before: object) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT COUNT(*) FROM tenants WHERE created_at < %(before)s",
                {"before": before},
            )
            return int_from_row(value)

    async def counts_by_created_day(self, start: object, end: object) -> dict[str, int]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT DATE(created_at) AS d, COUNT(*) AS c FROM tenants "
                "WHERE created_at >= %(start)s AND created_at <= %(end)s GROUP BY d",
                {"start": start, "end": end},
            )
            counts: dict[str, int] = {}
            for row in rows:
                counts[str(row["d"])] = int_from_row(row["c"])
            return counts

    async def clear_sso_domain_except(self, keep_tenant_id: UUID) -> None:
        """IP-mode helper: clear sso_domain and disable SSO on all other tenants."""
        async with self.session() as db:
            rows = await db.fetchall(
                "UPDATE tenants SET sso_domain = NULL, sso_enabled = FALSE "
                "WHERE id IS DISTINCT FROM %(keep_tenant_id)s AND "
                "(sso_domain IS NOT NULL OR sso_enabled IS TRUE) RETURNING id",
                {"keep_tenant_id": keep_tenant_id},
            )
        for row in rows:
            tenant_id = uuid_from_row(row["id"])
            memo_drop("tenant", tenant_id)
            await bump_tenant_cache(tenant_id)

    async def list_for_sso_regen(self) -> Sequence[TenantRecord]:
        """SSO-enabled tenants first, then by created_at (IP-mode domain assignment)."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tenants "
                "ORDER BY sso_enabled DESC NULLS LAST, created_at ASC NULLS LAST"
            )
            return [TenantRecord.from_row(row) for row in rows]

    async def deletion_preview(self, tenant_id: UUID) -> TenantDeletionPreview:
        """Count affected rows in one advisory snapshot without locking or cleanup."""
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT (SELECT COUNT(*) FROM users WHERE tenant_id = %(tid)s) AS users_count, "
                "(SELECT COUNT(*) FROM agents WHERE tenant_id = %(tid)s) AS agents_count, "
                "(SELECT COUNT(*) FROM agent_schedules "
                "WHERE agent_id IN (SELECT id FROM agents WHERE tenant_id = %(tid)s) "
                "OR created_by IN (SELECT id FROM users WHERE tenant_id = %(tid)s)) AS schedules_count",
                {"tid": tenant_id},
            )
            row = rows[0]
            return {
                "users_count": int_from_row(row["users_count"]),
                "agents_count": int_from_row(row["agents_count"]),
                "schedules_count": int_from_row(row["schedules_count"]),
            }

    async def delete_cascade(self, tenant_id: UUID) -> None:
        """Delete a tenant and all dependent rows in FK-safe order."""
        params = {"tid": tenant_id}
        user_ids: list[UUID] = []
        agent_ids: list[UUID] = []
        async with self.session() as db:
            from app.core.access_cache import drop_agent_acl_version
            from app.core.acl_locks import lock_tenants
            from app.core.session_cache import bump_user_sessions

            await lock_tenants(db, (tenant_id,))
            user_rows = await db.fetchall(
                "SELECT id FROM users WHERE tenant_id = %(tid)s",
                params,
            )
            agent_rows = await db.fetchall(
                "SELECT id FROM agents WHERE tenant_id = %(tid)s",
                params,
            )
            user_ids = [uuid_from_row(row["id"]) for row in user_rows]
            agent_ids = [uuid_from_row(row["id"]) for row in agent_rows]
            await execute_cleanup(db, TENANT_DELETE_CLEANUP_SQL, params)
            await bump_user_sessions(user_ids)
            for agent_id in agent_ids:
                memo_drop("agent", agent_id)
                await drop_agent_acl_version(agent_id)
            memo_drop("tenant", tenant_id)
            await bump_tenant_cache(tenant_id)


tenant_dao = TenantDAO()
