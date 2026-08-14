"""DAO for agent_templates (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar
from uuid import UUID

from app.core.json_types import int_from_row
from app.dao.base import BaseDAO
from app.records.template import AgentTemplateRecord

_TEMPLATE_COLUMNS = (
    "id",
    "name",
    "description",
    "icon",
    "category",
    "soul_template",
    "default_skills",
    "default_mcp_servers",
    "default_autonomy_policy",
    "capability_bullets",
    "bootstrap_content",
    "is_builtin",
    "created_by",
    "created_at",
)


class AgentTemplateDAO(BaseDAO[AgentTemplateRecord]):
    """DAO for agent template catalog rows."""

    table: ClassVar[str] = "agent_templates"
    columns: ClassVar[tuple[str, ...]] = _TEMPLATE_COLUMNS
    record_factory = staticmethod(AgentTemplateRecord.from_row)

    async def list_builtins(self) -> Sequence[AgentTemplateRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_templates "
                + "WHERE is_builtin IS TRUE ORDER BY created_at ASC NULLS LAST"
            )
            return [AgentTemplateRecord.from_row(row) for row in rows]

    async def get_builtin_by_name(self, name: str) -> AgentTemplateRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_templates "
                + "WHERE name = %(name)s AND is_builtin IS TRUE LIMIT 1",
                {"name": name},
            )
            return AgentTemplateRecord.from_row(row) if row else None

    async def count_agents_using(self, template_id: UUID) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT COUNT(*) FROM agents WHERE template_id = %(template_id)s",
                {"template_id": template_id},
            )
            return int_from_row(value)

    async def list_all_ordered(self) -> Sequence[AgentTemplateRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_templates ORDER BY is_builtin DESC, created_at ASC NULLS LAST"
            )
            return [AgentTemplateRecord.from_row(row) for row in rows]

    async def list_ordered_by_name(self, *, category: str | None = None) -> Sequence[AgentTemplateRecord]:
        params: dict[str, Any] = {}
        category_sql = ""
        if category:
            category_sql = " WHERE category = %(category)s"
            params["category"] = category
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_templates{category_sql} ORDER BY name",
                params,
            )
            return [AgentTemplateRecord.from_row(row) for row in rows]

    async def get_by_name(self, name: str) -> AgentTemplateRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_templates WHERE name = %(name)s LIMIT 1",
                {"name": name},
            )
            return AgentTemplateRecord.from_row(row) if row else None


agent_template_dao = AgentTemplateDAO()
