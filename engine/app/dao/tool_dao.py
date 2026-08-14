"""DAO for tools and agent_tools (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar
from uuid import UUID

from app.core.json_types import int_from_row, uuid_list_from_rows
from app.dao.base import BaseDAO
from app.records.tool import AgentToolRecord, ToolRecord

_TOOL_COLUMNS = (
    "id",
    "name",
    "display_name",
    "description",
    "type",
    "category",
    "icon",
    "parameters_schema",
    "config",
    "config_schema",
    "mcp_server_url",
    "mcp_server_name",
    "mcp_tool_name",
    "enabled",
    "is_default",
    "source",
    "tenant_id",
    "created_at",
    "updated_at",
)

_AGENT_TOOL_COLUMNS = (
    "id",
    "agent_id",
    "tool_id",
    "enabled",
    "config",
    "source",
    "installed_by_agent_id",
    "created_at",
)


class ToolDAO(BaseDAO[ToolRecord]):
    """DAO for the tools catalog."""

    table: ClassVar[str] = "tools"
    columns: ClassVar[tuple[str, ...]] = _TOOL_COLUMNS
    record_factory = staticmethod(ToolRecord.from_row)

    async def get_by_name(self, name: str) -> ToolRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM tools WHERE name = %(name)s",
                {"name": name},
            )
            return ToolRecord.from_row(row) if row else None

    async def list_by_names(self, names: Sequence[str]) -> Sequence[ToolRecord]:
        if not names:
            return []
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tools WHERE name = ANY(%(names)s)",
                {"names": list(names)},
            )
            return [ToolRecord.from_row(row) for row in rows]

    async def list_ids_by_names(self, names: Sequence[str]) -> Sequence[UUID]:
        if not names:
            return []
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id FROM tools WHERE name = ANY(%(names)s)",
                {"names": list(names)},
            )
            return uuid_list_from_rows(rows)

    async def list_enabled_by_category(self, category: str) -> Sequence[ToolRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tools "
                + "WHERE category = %(category)s AND enabled IS TRUE ORDER BY name",
                {"category": category},
            )
            return [ToolRecord.from_row(row) for row in rows]

    async def get_enabled_by_name(self, name: str) -> ToolRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM tools WHERE name = %(name)s AND enabled IS TRUE LIMIT 1",
                {"name": name},
            )
            return ToolRecord.from_row(row) if row else None

    async def get_mcp_by_mcp_tool_name(self, mcp_tool_name: str) -> ToolRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM tools "
                + "WHERE type = 'mcp' AND mcp_tool_name = %(mcp_tool_name)s LIMIT 1",
                {"mcp_tool_name": mcp_tool_name},
            )
            return ToolRecord.from_row(row) if row else None

    async def list_mcp_by_server_url(self, mcp_server_url: str) -> Sequence[ToolRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tools WHERE type = 'mcp' AND mcp_server_url = %(mcp_server_url)s",
                {"mcp_server_url": mcp_server_url},
            )
            return [ToolRecord.from_row(row) for row in rows]

    async def list_by_source(self, source: str) -> Sequence[ToolRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tools WHERE source = %(source)s",
                {"source": source},
            )
            return [ToolRecord.from_row(row) for row in rows]

    async def list_enabled_for_agent_catalog(
        self,
        *,
        agent_tenant_id: UUID | None,
        assigned_tool_ids: Sequence[UUID],
    ) -> Sequence[ToolRecord]:
        """Tools visible in the LLM catalog for an agent."""
        params: dict[str, Any] = {}
        clauses = [
            "enabled IS TRUE",
            "("
            + " source = 'builtin'"
            + " OR (source = 'admin' AND (tenant_id IS NULL"
            + (" OR tenant_id = %(tenant_id)s" if agent_tenant_id is not None else "")
            + "))"
            + (" OR id = ANY(%(assigned_ids)s)" if assigned_tool_ids else "")
            + ")",
        ]
        if agent_tenant_id is not None:
            params["tenant_id"] = agent_tenant_id
        if assigned_tool_ids:
            params["assigned_ids"] = list(assigned_tool_ids)
        where = " AND ".join(clauses)
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tools WHERE {where}",
                params or None,
            )
            return [ToolRecord.from_row(row) for row in rows]

    async def delete_orphan_mcp_tools(self, assigned_ids: Sequence[UUID]) -> int:
        async with self.session() as db:
            if assigned_ids:
                result = await db.fetchone(
                    "WITH deleted AS ("
                    + "  DELETE FROM tools "
                    + "  WHERE type = 'mcp' AND tenant_id IS NULL AND NOT (id = ANY(%(assigned_ids)s)) "
                    + "  RETURNING 1"
                    + ") SELECT COUNT(*) AS cnt FROM deleted",
                    {"assigned_ids": list(assigned_ids)},
                )
            else:
                result = await db.fetchone(
                    "WITH deleted AS ("
                    + "  DELETE FROM tools WHERE type = 'mcp' AND tenant_id IS NULL RETURNING 1"
                    + ") SELECT COUNT(*) AS cnt FROM deleted"
                )
            return int_from_row(result["cnt"] if result else 0)

    async def list_platform_for_tenant(self, tenant_id: UUID | None) -> Sequence[ToolRecord]:
        params: dict[str, Any] = {}
        tenant_sql = ""
        if tenant_id is not None:
            tenant_sql = " AND (tenant_id IS NULL OR tenant_id = %(tenant_id)s)"
            params["tenant_id"] = tenant_id
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tools "
                + f"WHERE source = ANY(%(sources)s){tenant_sql} "
                + "ORDER BY category, name",
                {**params, "sources": ["builtin", "admin"]},
            )
            return [ToolRecord.from_row(row) for row in rows]

    async def get_by_name_and_tenant(self, name: str, tenant_id: UUID | None) -> ToolRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM tools "
                + "WHERE name = %(name)s AND tenant_id IS NOT DISTINCT FROM %(tenant_id)s LIMIT 1",
                {"name": name, "tenant_id": tenant_id},
            )
            return ToolRecord.from_row(row) if row else None

    async def list_by_ids(self, tool_ids: Sequence[UUID]) -> Sequence[ToolRecord]:
        if not tool_ids:
            return []
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tools WHERE id = ANY(%(ids)s)",
                {"ids": list(tool_ids)},
            )
            return [ToolRecord.from_row(row) for row in rows]

    async def list_enabled_visible(
        self,
        *,
        agent_tenant_id: UUID | None,
        assigned_tool_ids: Sequence[UUID],
    ) -> Sequence[ToolRecord]:
        params: dict[str, Any] = {}
        clauses = [
            "enabled IS TRUE",
            "("
            + " source = 'builtin'"
            + " OR (source = 'admin' AND (tenant_id IS NULL"
            + (" OR tenant_id = %(tenant_id)s" if agent_tenant_id is not None else "")
            + "))"
            + (" OR id = ANY(%(assigned_ids)s)" if assigned_tool_ids else "")
            + ")",
        ]
        if agent_tenant_id is not None:
            params["tenant_id"] = agent_tenant_id
        if assigned_tool_ids:
            params["assigned_ids"] = list(assigned_tool_ids)
        where = " AND ".join(clauses)
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tools WHERE {where} ORDER BY category, name",
                params or None,
            )
            return [ToolRecord.from_row(row) for row in rows]

    async def list_by_mcp_server(self, server_name: str, tenant_id: UUID | None) -> Sequence[ToolRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tools "
                + "WHERE mcp_server_name = %(server_name)s AND tenant_id IS NOT DISTINCT FROM %(tenant_id)s",
                {"server_name": server_name, "tenant_id": tenant_id},
            )
            return [ToolRecord.from_row(row) for row in rows]

    async def list_enabled_by_category_visible(
        self,
        category: str,
        *,
        agent_tenant_id: UUID | None,
        assigned_tool_ids: Sequence[UUID],
        primary_tool_name: str | None = None,
    ) -> Sequence[ToolRecord]:
        tools = await self.list_enabled_visible(
            agent_tenant_id=agent_tenant_id,
            assigned_tool_ids=assigned_tool_ids,
        )
        filtered = [t for t in tools if t.category == category]
        if primary_tool_name:
            filtered.sort(key=lambda t: (t.name != primary_tool_name, t.name))
        else:
            filtered.sort(key=lambda t: t.name)
        return filtered

    async def top_enabled_categories(self, *, limit: int = 10) -> Sequence[dict[str, Any]]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT t.category, COUNT(*) AS count "
                + "FROM tools t JOIN agent_tools at ON at.tool_id = t.id "
                + "WHERE at.enabled IS TRUE "
                + "GROUP BY t.category ORDER BY COUNT(*) DESC LIMIT %(limit)s",
                {"limit": limit},
            )
            return [{"category": row["category"] or "uncategorized", "count": int_from_row(row.get("count"))} for row in rows]

    async def list_defaults(self) -> Sequence[ToolRecord]:
        async with self.session() as db:
            rows = await db.fetchall(f"SELECT {self._select_list()} FROM tools WHERE is_default IS TRUE")
            return [ToolRecord.from_row(row) for row in rows]

    async def list_mcp_by_server_display_name(self, display_name: str) -> Sequence[ToolRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tools WHERE mcp_server_name = %(display_name)s AND type = 'mcp'",
                {"display_name": display_name},
            )
            return [ToolRecord.from_row(row) for row in rows]

    async def list_mcp_by_name_prefixes(self, prefixes: Sequence[str]) -> Sequence[ToolRecord]:
        if not prefixes:
            return []
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tools WHERE type = 'mcp' AND name LIKE ANY(%(patterns)s)",
                {"patterns": [f"{p}%" for p in prefixes]},
            )
            return [ToolRecord.from_row(row) for row in rows]

    async def update_mcp_server_url(self, tool_id: UUID, mcp_server_url: str) -> ToolRecord | None:
        tool = await self.get(tool_id)
        if tool is None:
            return None
        return await self.update(db_obj=tool, obj_in={"mcp_server_url": mcp_server_url})

    async def update_config_for_mcp_server(self, server_name: str, config: dict[str, Any]) -> int:
        from app.db.types import as_jsonb

        async with self.session() as db:
            rows = await db.fetchall(
                "UPDATE tools SET config = %(config)s, updated_at = NOW() "
                + "WHERE mcp_server_name = %(server_name)s AND type = 'mcp' RETURNING id",
                {"server_name": server_name, "config": as_jsonb(config)},
            )
            return len(rows)


class AgentToolDAO(BaseDAO[AgentToolRecord]):
    """DAO for agent tool assignments."""

    table: ClassVar[str] = "agent_tools"
    columns: ClassVar[tuple[str, ...]] = _AGENT_TOOL_COLUMNS
    record_factory = staticmethod(AgentToolRecord.from_row)

    async def get_assignment(self, agent_id: UUID, tool_id: UUID) -> AgentToolRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_tools "
                + "WHERE agent_id = %(agent_id)s AND tool_id = %(tool_id)s",
                {"agent_id": agent_id, "tool_id": tool_id},
            )
            return AgentToolRecord.from_row(row) if row else None

    async def get_assignment_with_tool_by_name(
        self, agent_id: UUID, tool_name: str
    ) -> tuple[AgentToolRecord, dict[str, Any]] | None:
        """Return (assignment, tool row fields) for an agent+tool name join."""
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list('at')}, "
                + "t.config AS tool_config, t.config_schema AS tool_config_schema, "
                + "t.source AS tool_source, t.name AS tool_name "
                + "FROM agent_tools at "
                + "JOIN tools t ON t.id = at.tool_id "
                + "WHERE at.agent_id = %(agent_id)s AND t.name = %(tool_name)s "
                + "LIMIT 1",
                {"agent_id": agent_id, "tool_name": tool_name},
            )
            if not row:
                return None
            assignment = AgentToolRecord.from_row({col: row[col] for col in self.columns if col in row})
            tool_fields: dict[str, Any] = {
                "config": row.get("tool_config") or {},
                "config_schema": row.get("tool_config_schema") or {},
                "source": row.get("tool_source") or "builtin",
                "name": row.get("tool_name") or tool_name,
            }
            return assignment, tool_fields

    async def list_for_tool(self, tool_id: UUID) -> Sequence[AgentToolRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_tools WHERE tool_id = %(tool_id)s",
                {"tool_id": tool_id},
            )
            return [AgentToolRecord.from_row(row) for row in rows]

    async def list_for_agent(self, agent_id: UUID) -> Sequence[AgentToolRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_tools WHERE agent_id = %(agent_id)s",
                {"agent_id": agent_id},
            )
            return [AgentToolRecord.from_row(row) for row in rows]

    async def list_distinct_tool_ids(self) -> Sequence[UUID]:
        async with self.session() as db:
            rows = await db.fetchall("SELECT DISTINCT tool_id FROM agent_tools")
            return uuid_list_from_rows(rows, "tool_id")

    async def list_agent_ids_with_enabled_tools(self, tool_ids: Sequence[UUID]) -> Sequence[UUID]:
        if not tool_ids:
            return []
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT DISTINCT agent_id FROM agent_tools WHERE tool_id = ANY(%(tool_ids)s) AND enabled IS TRUE",
                {"tool_ids": list(tool_ids)},
            )
            return uuid_list_from_rows(rows, "agent_id")

    async def ensure_enabled(self, agent_id: UUID, tool_id: UUID) -> bool:
        """Create assignment if missing. Returns True when a new row was created."""
        existing = await self.get_assignment(agent_id, tool_id)
        if existing is not None:
            return False
        _ = await self.create(obj_in={"agent_id": agent_id, "tool_id": tool_id, "enabled": True})
        return True

    async def ensure_with_config(
        self,
        agent_id: UUID,
        tool_id: UUID,
        *,
        config: dict[str, Any] | None = None,
        source: str = "user_installed",
        installed_by_agent_id: UUID | None = None,
        merge_config: bool = True,
    ) -> AgentToolRecord:
        """Create or update an agent tool assignment with optional config."""
        existing = await self.get_assignment(agent_id, tool_id)
        if existing is not None:
            if config is None:
                return existing
            new_config = {**(existing.config or {}), **config} if merge_config else dict(config)
            return await self.update(db_obj=existing, obj_in={"config": new_config})
        return await self.create(
            obj_in={
                "agent_id": agent_id,
                "tool_id": tool_id,
                "enabled": True,
                "source": source,
                "installed_by_agent_id": installed_by_agent_id,
                "config": config or {},
            }
        )

    async def reassign_tool(self, *, from_tool_id: UUID, to_tool_id: UUID) -> None:
        """Move assignments from one tool to another (merge rename path)."""
        async with self.session() as db:
            await db.execute(
                """
                UPDATE agent_tools AS at
                SET tool_id = %(to_tool_id)s
                WHERE tool_id = %(from_tool_id)s
                  AND NOT EXISTS (
                    SELECT 1 FROM agent_tools x
                    WHERE x.agent_id = at.agent_id AND x.tool_id = %(to_tool_id)s
                  )
                """,
                {"from_tool_id": from_tool_id, "to_tool_id": to_tool_id},
            )
            await db.execute(
                "DELETE FROM agent_tools WHERE tool_id = %(from_tool_id)s",
                {"from_tool_id": from_tool_id},
            )

    async def delete_for_tool(self, tool_id: UUID) -> None:
        async with self.session() as db:
            await db.execute("DELETE FROM agent_tools WHERE tool_id = %(tool_id)s", {"tool_id": tool_id})

    async def list_agent_installed(self, tenant_id: str | UUID | None) -> Sequence[dict[str, Any]]:
        params: dict[str, Any] = {}
        tenant_sql = ""
        if tenant_id is not None:
            tenant_sql = " AND owner.tenant_id::text = %(tenant_id)s"
            params["tenant_id"] = str(tenant_id)
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT at.id AS agent_tool_id, at.agent_id, at.tool_id, at.enabled, at.config, "
                + "at.installed_by_agent_id, at.created_at AS installed_at, "
                + "t.name AS tool_name, t.display_name AS tool_display_name, t.description, "
                + "t.type, t.category, t.source, t.mcp_server_name, t.mcp_server_url, t.mcp_tool_name, "
                + "installer.name AS installed_by_agent_name "
                + "FROM agent_tools at "
                + "JOIN tools t ON t.id = at.tool_id "
                + "JOIN agents owner ON owner.id = at.agent_id "
                + "LEFT JOIN agents installer ON installer.id = at.installed_by_agent_id "
                + "WHERE (at.source = 'user_installed' OR t.source = 'agent')"
                + f"{tenant_sql} "
                + "ORDER BY at.created_at DESC NULLS LAST",
                params or None,
            )
            return list(rows)


tool_dao = ToolDAO()
agent_tool_dao = AgentToolDAO()
