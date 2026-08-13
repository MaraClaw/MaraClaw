"""DAO for agents and agent_permissions (psycopg)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from app.core.access_cache import bump_agent_acl_version, drop_agent_acl_version
from app.dao.base import BaseDAO
from app.records.agent import AgentPermissionRecord, AgentRecord

_AGENT_POLICY_COLUMNS = frozenset({"access_mode", "company_access_level", "creator_id", "tenant_id"})

_AGENT_COLUMNS = (
    "id",
    "name",
    "avatar_url",
    "role_description",
    "bio",
    "welcome_message",
    "creator_id",
    "tenant_id",
    "agent_type",
    "gogcli_enabled",
    "api_key_hash",
    "openclaw_last_seen",
    "status",
    "container_id",
    "container_port",
    "primary_model_id",
    "fallback_model_id",
    "autonomy_policy",
    "max_tokens_per_day",
    "max_tokens_per_month",
    "tokens_used_today",
    "tokens_used_month",
    "last_daily_reset",
    "last_monthly_reset",
    "tokens_used_total",
    "cache_read_tokens_today",
    "cache_read_tokens_month",
    "cache_read_tokens_total",
    "cache_creation_tokens_today",
    "cache_creation_tokens_month",
    "cache_creation_tokens_total",
    "context_window_size",
    "max_tool_rounds",
    "max_triggers",
    "min_poll_interval_min",
    "webhook_rate_limit",
    "expires_at",
    "is_expired",
    "is_system",
    "access_mode",
    "company_access_level",
    "llm_calls_today",
    "max_llm_calls_per_day",
    "llm_calls_reset_at",
    "template_id",
    "heartbeat_enabled",
    "heartbeat_interval_minutes",
    "heartbeat_active_hours",
    "last_heartbeat_at",
    "timezone",
    "created_at",
    "updated_at",
    "last_active_at",
)

_PERM_COLUMNS = ("id", "agent_id", "scope_type", "scope_id", "access_level")

_HEARTBEAT_CANDIDATE_COLUMNS = (
    "id",
    "name",
    "creator_id",
    "tenant_id",
    "status",
    "expires_at",
    "is_expired",
    "heartbeat_enabled",
    "heartbeat_interval_minutes",
    "heartbeat_active_hours",
    "last_heartbeat_at",
    "timezone",
)


class AgentDAO(BaseDAO[AgentRecord]):
    """DAO for Agent records."""

    table = "agents"
    columns = _AGENT_COLUMNS
    record_factory = staticmethod(AgentRecord.from_row)

    async def update(self, *, db_obj: AgentRecord, obj_in: Mapping[str, Any]) -> AgentRecord:
        updated = await super().update(db_obj=db_obj, obj_in=obj_in)
        if _AGENT_POLICY_COLUMNS.intersection(obj_in):
            await bump_agent_acl_version(updated.id)
        return updated

    async def delete(self, *, id: Any) -> AgentRecord | None:
        deleted = await super().delete(id=id)
        if deleted is not None:
            await drop_agent_acl_version(deleted.id)
        return deleted

    async def get_by_name(self, name: str) -> AgentRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agents WHERE name = %(name)s LIMIT 1",
                {"name": name},
            )
            return AgentRecord.from_row(row) if row else None

    async def get_system_by_name(self, tenant_id: UUID, name: str) -> AgentRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agents "
                "WHERE tenant_id = %(tenant_id)s AND is_system IS TRUE AND name = %(name)s LIMIT 1",
                {"tenant_id": tenant_id, "name": name},
            )
            return AgentRecord.from_row(row) if row else None

    async def get_system_by_name_any(self, name: str) -> AgentRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agents "
                "WHERE is_system IS TRUE AND name = %(name)s AND status <> 'stopped' LIMIT 1",
                {"name": name},
            )
            return AgentRecord.from_row(row) if row else None

    async def list_system_by_name(self, name: str, *, exclude_stopped: bool = True) -> Sequence[AgentRecord]:
        params: dict[str, Any] = {"name": name}
        status_sql = " AND status <> 'stopped'" if exclude_stopped else ""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agents "
                f"WHERE is_system IS TRUE AND name = %(name)s{status_sql} "
                "ORDER BY created_at DESC NULLS LAST",
                params,
            )
            return [AgentRecord.from_row(row) for row in rows]

    async def list_by_name_any(self, name: str, *, exclude_stopped: bool = True) -> Sequence[AgentRecord]:
        params: dict[str, Any] = {"name": name}
        status_sql = " AND status <> 'stopped'" if exclude_stopped else ""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agents "
                f"WHERE name = %(name)s{status_sql} ORDER BY created_at DESC NULLS LAST",
                params,
            )
            return [AgentRecord.from_row(row) for row in rows]

    async def list_visible_for_user(
        self,
        *,
        user_id: UUID,
        tenant_id: UUID | None,
        role: str,
        exclude_agent_id: UUID | None = None,
        search: str | None = None,
        limit: int | None = None,
    ) -> Sequence[AgentRecord]:
        """Return agents visible under the same rules as build_visible_agents_query."""
        if tenant_id is None:
            return []

        params: dict[str, Any] = {"tenant_id": tenant_id, "user_id": user_id}
        exclude_sql = ""
        if exclude_agent_id is not None:
            exclude_sql = " AND a.id <> %(exclude_agent_id)s"
            params["exclude_agent_id"] = exclude_agent_id

        search_sql = ""
        if search:
            search_sql = " AND (a.name ILIKE %(search)s OR COALESCE(a.role_description, '') ILIKE %(search)s)"
            params["search"] = f"%{search}%"

        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT %(limit)s"
            params["limit"] = limit

        if role in ("platform_admin", "org_admin"):
            sql = (
                f"SELECT {self._select_list('a')} FROM agents a "
                "WHERE a.tenant_id = %(tenant_id)s "
                "AND (a.creator_id = %(user_id)s OR a.access_mode <> 'private')"
                f"{exclude_sql}{search_sql} ORDER BY a.created_at DESC NULLS LAST{limit_sql}"
            )
        else:
            sql = (
                f"SELECT {self._select_list('a')} FROM agents a "
                "WHERE a.tenant_id = %(tenant_id)s AND ("
                " a.creator_id = %(user_id)s"
                " OR a.access_mode = 'company'"
                " OR a.id IN ("
                "   SELECT ap.agent_id FROM agent_permissions ap"
                "   WHERE ap.scope_type = 'user' AND ap.scope_id = %(user_id)s"
                " )"
                f"){exclude_sql}{search_sql} ORDER BY a.created_at DESC NULLS LAST{limit_sql}"
            )

        async with self.session() as db:
            rows = await db.fetchall(sql, params)
            return [AgentRecord.from_row(row) for row in rows]

    async def get_openclaw_by_api_key(self, api_key: str) -> AgentRecord | None:
        """Resolve an OpenClaw agent by plaintext or legacy hashed API key."""
        import hashlib

        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agents "
                "WHERE api_key_hash = %(key)s AND agent_type = 'openclaw' LIMIT 1",
                {"key": api_key},
            )
            if row:
                return AgentRecord.from_row(row)
            key_hash = hashlib.sha256(api_key.encode()).hexdigest()
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agents "
                "WHERE api_key_hash = %(key)s AND agent_type = 'openclaw' LIMIT 1",
                {"key": key_hash},
            )
            return AgentRecord.from_row(row) if row else None

    async def list_all_ids(self) -> Sequence[UUID]:
        async with self.session() as db:
            rows = await db.fetchall("SELECT id FROM agents")
            return [row["id"] for row in rows]

    async def list_ids_for_creator(self, creator_id: UUID) -> Sequence[UUID]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id FROM agents WHERE creator_id = %(creator_id)s",
                {"creator_id": creator_id},
            )
            return [row["id"] for row in rows]

    async def apply_token_counter_resets(self, agent: AgentRecord) -> AgentRecord | None:
        """Persist lazy daily/monthly token counter resets when needed."""
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        updates: dict[str, Any] = {}

        last_daily = agent.last_daily_reset
        if last_daily is None or last_daily.date() < now.date():
            updates["tokens_used_today"] = 0
            updates["cache_read_tokens_today"] = 0
            updates["cache_creation_tokens_today"] = 0
            updates["last_daily_reset"] = now

        last_monthly = agent.last_monthly_reset
        if last_monthly is None or (last_monthly.year, last_monthly.month) < (now.year, now.month):
            updates["tokens_used_month"] = 0
            updates["cache_read_tokens_month"] = 0
            updates["cache_creation_tokens_month"] = 0
            updates["last_monthly_reset"] = now

        if not updates:
            return None
        return await self.update(db_obj=agent, obj_in=updates)

    async def apply_token_counter_resets_many(self, agents: Sequence[AgentRecord]) -> None:
        """Reset daily/monthly counters for many agents in at most two UPDATEs."""
        from datetime import UTC, datetime

        if not agents:
            return
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        daily_ids: list[UUID] = []
        monthly_ids: list[UUID] = []
        for agent in agents:
            last_daily = agent.last_daily_reset
            if last_daily is None or last_daily.date() < now.date():
                daily_ids.append(agent.id)
            last_monthly = agent.last_monthly_reset
            if last_monthly is None or (last_monthly.year, last_monthly.month) < (now.year, now.month):
                monthly_ids.append(agent.id)
        if not daily_ids and not monthly_ids:
            return
        async with self.session() as db:
            if daily_ids:
                await db.execute(
                    "UPDATE agents SET tokens_used_today = 0, cache_read_tokens_today = 0, "
                    "cache_creation_tokens_today = 0, last_daily_reset = %(now)s "
                    "WHERE id = ANY(%(ids)s) AND (last_daily_reset IS NULL OR last_daily_reset::date < %(today)s)",
                    {"now": now, "ids": daily_ids, "today": now.date()},
                )
            if monthly_ids:
                await db.execute(
                    "UPDATE agents SET tokens_used_month = 0, cache_read_tokens_month = 0, "
                    "cache_creation_tokens_month = 0, last_monthly_reset = %(now)s "
                    "WHERE id = ANY(%(ids)s) AND (last_monthly_reset IS NULL OR last_monthly_reset < %(month_start)s)",
                    {"now": now, "ids": monthly_ids, "month_start": month_start},
                )
        daily_set = set(daily_ids)
        monthly_set = set(monthly_ids)
        for agent in agents:
            if agent.id in daily_set:
                agent.tokens_used_today = 0
                agent.cache_read_tokens_today = 0
                agent.cache_creation_tokens_today = 0
                agent.last_daily_reset = now
            if agent.id in monthly_set:
                agent.tokens_used_month = 0
                agent.cache_read_tokens_month = 0
                agent.cache_creation_tokens_month = 0
                agent.last_monthly_reset = now

    async def list_heartbeat_candidates(self) -> Sequence[AgentRecord]:
        """Agents with heartbeat enabled and a runnable status (claim columns only)."""
        cols = ", ".join(_HEARTBEAT_CANDIDATE_COLUMNS)
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {cols} FROM agents WHERE heartbeat_enabled IS TRUE AND status = ANY(%(statuses)s)",
                {"statuses": ["running", "idle"]},
            )
            return [AgentRecord.from_row(row) for row in rows]

    async def claim_heartbeat(
        self,
        agent_id: UUID,
        *,
        now: Any,
        interval,
    ) -> bool:
        """Atomically claim a heartbeat slot; return True when this process won the claim."""
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE agents SET last_heartbeat_at = %(now)s "
                "WHERE id = %(id)s "
                "AND heartbeat_enabled IS TRUE "
                "AND status = ANY(%(statuses)s) "
                "AND (last_heartbeat_at IS NULL OR last_heartbeat_at <= %(cutoff)s) "
                "RETURNING id",
                {
                    "id": agent_id,
                    "now": now,
                    "statuses": ["running", "idle"],
                    "cutoff": now - interval,
                },
            )
            return row is not None

    async def mark_expired_stopped(self, agent: AgentRecord) -> AgentRecord | None:
        return await self.update(
            db_obj=agent,
            obj_in={"is_expired": True, "heartbeat_enabled": False, "status": "stopped"},
        )

    async def count_active_for_creator(self, creator_id: UUID) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT COUNT(*) FROM agents WHERE creator_id = %(creator_id)s AND is_expired IS FALSE",
                {"creator_id": creator_id},
            )
            return int(value or 0)

    async def is_hidden_from_plaza(self, agent_id: UUID) -> bool:
        """True when agent is system or not company-public (excluded from plaza feed)."""
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT 1 FROM agents WHERE id = %(id)s "
                "AND (is_system IS TRUE OR COALESCE(access_mode, 'company') <> 'company') LIMIT 1",
                {"id": agent_id},
            )
            return value is not None

    async def list_hidden_from_plaza_ids(self, agent_ids: Sequence[UUID]) -> set[UUID]:
        if not agent_ids:
            return set()
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id FROM agents WHERE id = ANY(%(ids)s) "
                "AND (is_system IS TRUE OR COALESCE(access_mode, 'company') <> 'company')",
                {"ids": list(agent_ids)},
            )
            return {row["id"] for row in rows}

    async def list_for_tenant(self, tenant_id: UUID) -> Sequence[AgentRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agents WHERE tenant_id = %(tenant_id)s",
                {"tenant_id": tenant_id},
            )
            return [AgentRecord.from_row(row) for row in rows]

    async def list_active_nonsystem_for_tenant(
        self,
        tenant_id: UUID,
        *,
        exclude_id: UUID | None = None,
        company_only: bool = False,
    ) -> Sequence[AgentRecord]:
        params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "exclude_statuses": ["stopped", "error"],
        }
        clauses = [
            "tenant_id = %(tenant_id)s",
            "is_system IS FALSE",
            "NOT (status = ANY(%(exclude_statuses)s))",
        ]
        if exclude_id is not None:
            clauses.append("id <> %(exclude_id)s")
            params["exclude_id"] = exclude_id
        if company_only:
            clauses.append("access_mode = 'company'")
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agents WHERE {' AND '.join(clauses)}",
                params,
            )
            return [AgentRecord.from_row(row) for row in rows]

    async def list_id_name_avatar_active_nonsystem(self, tenant_id: UUID) -> Sequence[dict[str, Any]]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id, name, avatar_url FROM agents "
                "WHERE tenant_id = %(tenant_id)s AND is_system IS FALSE "
                "AND NOT (status = ANY(%(exclude_statuses)s))",
                {"tenant_id": tenant_id, "exclude_statuses": ["stopped", "error"]},
            )
            return list(rows)

    async def list_by_names_for_tenant(
        self,
        tenant_id: UUID,
        names: Sequence[str],
        *,
        agent_type: str | None = None,
        exclude_stopped: bool = True,
    ) -> Sequence[AgentRecord]:
        if not names:
            return []
        params: dict[str, Any] = {"tenant_id": tenant_id, "names": list(names)}
        clauses = ["tenant_id = %(tenant_id)s", "name = ANY(%(names)s)"]
        if agent_type is not None:
            clauses.append("agent_type = %(agent_type)s")
            params["agent_type"] = agent_type
        if exclude_stopped:
            clauses.append("status <> 'stopped'")
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agents "
                f"WHERE {' AND '.join(clauses)} ORDER BY created_at ASC NULLS LAST",
                params,
            )
            return [AgentRecord.from_row(row) for row in rows]

    async def list_by_status(self, status: str) -> Sequence[AgentRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agents WHERE status = %(status)s",
                {"status": status},
            )
            return [AgentRecord.from_row(row) for row in rows]

    async def list_by_statuses(
        self, statuses: Sequence[str], *, exclude_id: UUID | None = None
    ) -> Sequence[AgentRecord]:
        params: dict[str, Any] = {"statuses": list(statuses)}
        exclude_sql = ""
        if exclude_id is not None:
            exclude_sql = " AND id <> %(exclude_id)s"
            params["exclude_id"] = exclude_id
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agents WHERE status = ANY(%(statuses)s){exclude_sql} ORDER BY name",
                params,
            )
            return [AgentRecord.from_row(row) for row in rows]

    async def raise_heartbeat_floor(self, tenant_id: UUID, floor: int) -> int:
        """Raise heartbeat intervals below floor; return number of agents adjusted."""
        async with self.session() as db:
            rows = await db.fetchall(
                "UPDATE agents SET heartbeat_interval_minutes = %(floor)s, updated_at = NOW() "
                "WHERE tenant_id = %(tenant_id)s AND heartbeat_interval_minutes < %(floor)s "
                "RETURNING id",
                {"tenant_id": tenant_id, "floor": floor},
            )
            return len(rows)

    async def increment_token_usage(
        self,
        agent_id: UUID,
        *,
        total_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> AgentRecord | None:
        """Atomically add token counters; return the refreshed agent row."""
        async with self.session() as db:
            row = await db.fetchone(
                f"UPDATE agents SET "
                "tokens_used_today = COALESCE(tokens_used_today, 0) + %(total)s, "
                "tokens_used_month = COALESCE(tokens_used_month, 0) + %(total)s, "
                "tokens_used_total = COALESCE(tokens_used_total, 0) + %(total)s, "
                "cache_read_tokens_today = COALESCE(cache_read_tokens_today, 0) + %(cache_read)s, "
                "cache_read_tokens_month = COALESCE(cache_read_tokens_month, 0) + %(cache_read)s, "
                "cache_read_tokens_total = COALESCE(cache_read_tokens_total, 0) + %(cache_read)s, "
                "cache_creation_tokens_today = COALESCE(cache_creation_tokens_today, 0) + %(cache_creation)s, "
                "cache_creation_tokens_month = COALESCE(cache_creation_tokens_month, 0) + %(cache_creation)s, "
                "cache_creation_tokens_total = COALESCE(cache_creation_tokens_total, 0) + %(cache_creation)s, "
                "updated_at = NOW() "
                f"WHERE id = %(id)s RETURNING {self._select_list()}",
                {
                    "id": agent_id,
                    "total": total_tokens,
                    "cache_read": cache_read_tokens,
                    "cache_creation": cache_creation_tokens,
                },
            )
            return AgentRecord.from_row(row) if row else None

    async def count_for_tenant(self, tenant_id: UUID, *, status: str | None = None) -> int:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        status_sql = ""
        if status is not None:
            status_sql = " AND status = %(status)s"
            params["status"] = status
        async with self.session() as db:
            value = await db.fetchval(
                f"SELECT COUNT(*) FROM agents WHERE tenant_id = %(tenant_id)s{status_sql}",
                params,
            )
            return int(value or 0)

    async def sum_tokens_for_tenant(self, tenant_id: UUID) -> tuple[int, int]:
        async with self.session() as db:
            row = await db.fetchone(
                "SELECT COALESCE(SUM(tokens_used_total), 0) AS total, "
                "COALESCE(SUM(cache_read_tokens_total), 0) AS cache_read "
                "FROM agents WHERE tenant_id = %(tenant_id)s",
                {"tenant_id": tenant_id},
            )
            if not row:
                return 0, 0
            return int(row["total"] or 0), int(row["cache_read"] or 0)

    async def pause_running_for_tenant(self, tenant_id: UUID) -> int:
        async with self.session() as db:
            rows = await db.fetchall(
                "UPDATE agents SET status = 'paused', updated_at = NOW() "
                "WHERE tenant_id = %(tenant_id)s AND status = 'running' RETURNING id",
                {"tenant_id": tenant_id},
            )
            return len(rows)

    async def token_usage_for_tenant(self, tenant_id: UUID) -> dict[str, int]:
        async with self.session() as db:
            row = await db.fetchone(
                "SELECT "
                "COALESCE(SUM(tokens_used_today), 0) AS tokens_today, "
                "COALESCE(SUM(tokens_used_month), 0) AS tokens_month, "
                "COALESCE(SUM(tokens_used_total), 0) AS tokens_total, "
                "COALESCE(SUM(cache_read_tokens_today), 0) AS cache_today, "
                "COALESCE(SUM(cache_read_tokens_month), 0) AS cache_month, "
                "COALESCE(SUM(cache_read_tokens_total), 0) AS cache_total, "
                "COALESCE(SUM(cache_creation_tokens_today), 0) AS cache_creation_today, "
                "COALESCE(SUM(cache_creation_tokens_month), 0) AS cache_creation_month, "
                "COALESCE(SUM(cache_creation_tokens_total), 0) AS cache_creation_total "
                "FROM agents WHERE tenant_id = %(tenant_id)s",
                {"tenant_id": tenant_id},
            )
            if not row:
                return {
                    "tokens_today": 0,
                    "tokens_month": 0,
                    "tokens_total": 0,
                    "cache_today": 0,
                    "cache_month": 0,
                    "cache_total": 0,
                    "cache_creation_today": 0,
                    "cache_creation_month": 0,
                    "cache_creation_total": 0,
                }
            return {k: int(row[k] or 0) for k in row}

    async def sum_tokens_created_before(self, before: Any) -> tuple[int, int]:
        async with self.session() as db:
            row = await db.fetchone(
                "SELECT COALESCE(SUM(tokens_used_total), 0) AS total, "
                "COALESCE(SUM(cache_read_tokens_total), 0) AS cache_read "
                "FROM agents WHERE created_at < %(before)s",
                {"before": before},
            )
            if not row:
                return 0, 0
            return int(row["total"] or 0), int(row["cache_read"] or 0)

    async def top_token_companies(self, *, limit: int = 20) -> Sequence[dict[str, Any]]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT t.name, "
                "COALESCE(SUM(a.tokens_used_total), 0) AS total, "
                "COALESCE(SUM(a.cache_read_tokens_total), 0) AS cache_read "
                "FROM tenants t JOIN agents a ON a.tenant_id = t.id "
                "GROUP BY t.id, t.name "
                "ORDER BY SUM(a.tokens_used_total) DESC NULLS LAST "
                "LIMIT %(limit)s",
                {"limit": limit},
            )
            return [
                {
                    "name": row["name"],
                    "total": int(row["total"] or 0),
                    "cache_read": int(row["cache_read"] or 0),
                }
                for row in rows
            ]

    async def top_token_agents(self, *, limit: int = 20) -> Sequence[dict[str, Any]]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT a.name, t.name AS tenant_name, a.tokens_used_total, a.cache_read_tokens_total "
                "FROM agents a JOIN tenants t ON t.id = a.tenant_id "
                "ORDER BY a.tokens_used_total DESC NULLS LAST LIMIT %(limit)s",
                {"limit": limit},
            )
            return [
                {
                    "name": row["name"],
                    "company": row["tenant_name"],
                    "tokens": int(row["tokens_used_total"] or 0),
                    "cache_read_tokens": int(row["cache_read_tokens_total"] or 0),
                }
                for row in rows
            ]

    async def delete_with_related(self, agent_id: UUID) -> None:
        """Delete an agent and all known FK-dependent rows in one connection."""
        params = {"aid": agent_id}
        async with self.session() as db:
            for sql in AGENT_DELETE_CLEANUP_SQL:
                await db.execute(sql, params)
            await db.execute("DELETE FROM agents WHERE id = %(aid)s", params)

    async def names_referencing_model(self, model_id: UUID) -> list[str]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT name FROM agents "
                "WHERE primary_model_id = %(model_id)s OR fallback_model_id = %(model_id)s "
                "ORDER BY name",
                {"model_id": model_id},
            )
            return [row["name"] for row in rows if row.get("name")]

    async def nullify_model_references(self, model_id: UUID) -> None:
        async with self.session() as db:
            await db.execute(
                "UPDATE agents SET primary_model_id = NULL WHERE primary_model_id = %(model_id)s",
                {"model_id": model_id},
            )
            await db.execute(
                "UPDATE agents SET fallback_model_id = NULL WHERE fallback_model_id = %(model_id)s",
                {"model_id": model_id},
            )

    async def migrate_primary_model(
        self,
        *,
        tenant_id: UUID,
        old_model_id: UUID,
        new_model_id: UUID,
    ) -> int:
        async with self.session() as db:
            rows = await db.fetchall(
                "UPDATE agents SET primary_model_id = %(new_model_id)s "
                "WHERE tenant_id = %(tenant_id)s AND primary_model_id = %(old_model_id)s "
                "RETURNING id",
                {
                    "tenant_id": tenant_id,
                    "old_model_id": old_model_id,
                    "new_model_id": new_model_id,
                },
            )
            return len(rows)

    async def names_for_ids(self, agent_ids: Sequence[UUID]) -> dict[UUID, str]:
        if not agent_ids:
            return {}
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id, name FROM agents WHERE id = ANY(%(ids)s)",
                {"ids": list(agent_ids)},
            )
            return {row["id"]: row["name"] for row in rows}


# Ordered cleanup before deleting the agents row (FK dependents).
AGENT_DELETE_CLEANUP_SQL: tuple[str, ...] = (
    "DELETE FROM agent_activity_logs WHERE agent_id = %(aid)s",
    "DELETE FROM audit_logs WHERE agent_id = %(aid)s",
    "DELETE FROM approval_requests WHERE agent_id = %(aid)s",
    "DELETE FROM chat_messages WHERE agent_id = %(aid)s",
    "DELETE FROM chat_sessions WHERE agent_id = %(aid)s",
    "DELETE FROM agent_schedules WHERE agent_id = %(aid)s",
    "DELETE FROM agent_triggers WHERE agent_id = %(aid)s",
    "DELETE FROM channel_configs WHERE agent_id = %(aid)s",
    "DELETE FROM agent_permissions WHERE agent_id = %(aid)s",
    "DELETE FROM agent_tools WHERE agent_id = %(aid)s",
    "DELETE FROM agent_relationships WHERE agent_id = %(aid)s",
    "DELETE FROM gateway_messages WHERE agent_id = %(aid)s",
    "DELETE FROM published_pages WHERE agent_id = %(aid)s",
    "DELETE FROM notifications WHERE agent_id = %(aid)s",
    "DELETE FROM daily_token_usage WHERE agent_id = %(aid)s",
    "DELETE FROM task_logs WHERE task_id IN (SELECT id FROM tasks WHERE agent_id = %(aid)s)",
    "DELETE FROM tasks WHERE agent_id = %(aid)s",
    "DELETE FROM chat_sessions WHERE peer_agent_id = %(aid)s",
    "DELETE FROM gateway_messages WHERE sender_agent_id = %(aid)s",
    "UPDATE chat_messages SET sender_agent_id = NULL WHERE sender_agent_id = %(aid)s",
    "DELETE FROM agent_agent_relationships WHERE agent_id = %(aid)s OR target_agent_id = %(aid)s",
    "DELETE FROM plaza_posts WHERE author_id = %(aid)s",
    "DELETE FROM participants WHERE type = 'agent' AND ref_id = %(aid)s",
)


class AgentPermissionDAO(BaseDAO[AgentPermissionRecord]):
    """DAO for AgentPermission records."""

    table = "agent_permissions"
    columns = _PERM_COLUMNS
    record_factory = staticmethod(AgentPermissionRecord.from_row)

    async def create(self, *, obj_in: Mapping[str, Any]) -> AgentPermissionRecord:
        created = await super().create(obj_in=obj_in)
        await bump_agent_acl_version(created.agent_id)
        return created

    async def update(self, *, db_obj: AgentPermissionRecord, obj_in: Mapping[str, Any]) -> AgentPermissionRecord:
        updated = await super().update(db_obj=db_obj, obj_in=obj_in)
        await bump_agent_acl_version(updated.agent_id)
        return updated

    async def delete(self, *, id: Any) -> AgentPermissionRecord | None:
        deleted = await super().delete(id=id)
        if deleted is not None:
            await bump_agent_acl_version(deleted.agent_id)
        return deleted

    async def list_for_agent(self, agent_id: UUID) -> Sequence[AgentPermissionRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_permissions WHERE agent_id = %(agent_id)s",
                {"agent_id": agent_id},
            )
            return [AgentPermissionRecord.from_row(row) for row in rows]

    async def list_user_scope_ids(self, agent_id: UUID) -> Sequence[UUID]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT scope_id FROM agent_permissions "
                "WHERE agent_id = %(agent_id)s AND scope_type = 'user' AND scope_id IS NOT NULL",
                {"agent_id": agent_id},
            )
            return [row["scope_id"] for row in rows if row.get("scope_id") is not None]

    async def delete_for_agent(self, agent_id: UUID) -> None:
        async with self.session() as db:
            await db.execute(
                "DELETE FROM agent_permissions WHERE agent_id = %(agent_id)s",
                {"agent_id": agent_id},
            )
        await bump_agent_acl_version(agent_id)


agent_dao = AgentDAO()
agent_permission_dao = AgentPermissionDAO()
