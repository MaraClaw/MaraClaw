"""DAO for agent_focus_items"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, final
from uuid import UUID

from app.core.json_types import int_from_row
from app.dao.base import BaseDAO
from app.db.types import as_jsonb
from app.records.focus import AgentFocusItemRecord

_COLUMNS = (
    "id",
    "agent_id",
    "key",
    "title",
    "description",
    "status",
    "kind",
    "source",
    "metadata",
    "sort_order",
    "completed_at",
    "created_at",
    "updated_at",
)


@final
class AgentFocusItemDAO(BaseDAO[AgentFocusItemRecord]):
    table: ClassVar[str] = "agent_focus_items"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(AgentFocusItemRecord.from_row)

    def _select_list(self, alias: str | None = None) -> str:
        # Map SQL column "metadata" into a consistent select list.
        cols = []
        for col in self.columns:
            expr = f"{alias}.{col}" if alias else col
            cols.append(expr)
        return ", ".join(cols)

    async def count_for_agent(self, agent_id: UUID) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT COUNT(*) FROM agent_focus_items WHERE agent_id = %(agent_id)s",
                {"agent_id": agent_id},
            )
            return int_from_row(value)

    async def list_for_agent(self, agent_id: UUID, *, include_completed: bool = True) -> Sequence[AgentFocusItemRecord]:
        params: dict[str, Any] = {"agent_id": agent_id}
        completed_sql = "" if include_completed else " AND status <> 'completed'"
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_focus_items "
                + f"WHERE agent_id = %(agent_id)s{completed_sql} "
                + "ORDER BY status DESC, kind DESC, sort_order ASC, created_at ASC",
                params,
            )
            return [AgentFocusItemRecord.from_row(row) for row in rows]

    async def get_by_key(self, agent_id: UUID, key: str) -> AgentFocusItemRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_focus_items WHERE agent_id = %(agent_id)s AND key = %(key)s",
                {"agent_id": agent_id, "key": key},
            )
            return AgentFocusItemRecord.from_row(row) if row else None

    async def max_sort_order(self, agent_id: UUID) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT MAX(sort_order) FROM agent_focus_items WHERE agent_id = %(agent_id)s",
                {"agent_id": agent_id},
            )
            return int_from_row(value)

    async def bulk_insert_ignore(self, rows: list[dict[str, object]]) -> int:
        if not rows:
            return 0
        inserted = 0
        async with self.session() as db:
            for row in rows:
                data = dict(row)
                if "item_metadata" in data and "metadata" not in data:
                    data["metadata"] = data.pop("item_metadata")
                params = {}
                for col in self.columns:
                    if col not in data and col != "id":
                        continue
                    if col == "id" and col not in data:
                        from uuid import uuid4

                        data[col] = uuid4()
                    if col not in data:
                        continue
                    val: object = data[col]
                    params[col] = as_jsonb(val) if isinstance(val, (dict, list)) else val
                cols = [c for c in self.columns if c in params]
                col_sql = ", ".join(cols)
                val_sql = ", ".join(f"%({c})s" for c in cols)
                result = await db.fetchone(
                    f"INSERT INTO agent_focus_items ({col_sql}) VALUES ({val_sql}) "
                    + "ON CONFLICT (agent_id, key) DO NOTHING RETURNING id",
                    params,
                )
                if result:
                    inserted += 1
        return inserted

    async def update(self, *, db_obj: AgentFocusItemRecord, obj_in: Mapping[str, Any]) -> AgentFocusItemRecord:
        data = dict(obj_in)
        if "item_metadata" in data:
            data["metadata"] = data.pop("item_metadata")
        return await super().update(db_obj=db_obj, obj_in=data)

    async def create(self, *, obj_in: Mapping[str, Any]) -> AgentFocusItemRecord:
        data = dict(obj_in)
        if "item_metadata" in data:
            data["metadata"] = data.pop("item_metadata")
        return await super().create(obj_in=data)


agent_focus_item_dao = AgentFocusItemDAO()
