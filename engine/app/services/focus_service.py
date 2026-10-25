"""Structured Focus service backed exclusively by the focus DAO."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import TypedDict

from app.core.domain.focus import FocusKind, FocusStatus
from app.core.json_types import JsonObject
from app.dao.focus_dao import agent_focus_item_dao
from app.records.focus import AgentFocusItemRecord


class SerializedFocusItem(TypedDict):
    id: str
    agent_id: str
    key: str
    title: str | None
    description: str
    status: FocusStatus
    kind: FocusKind
    source: str
    metadata: JsonObject
    sort_order: int
    completed_at: str | None
    created_at: str | None
    updated_at: str | None


def slugify_focus_key(value: str, *, fallback: str = "focus") -> str:
    """Create a stable ASCII-ish key usable as focus_ref."""
    raw = (value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", raw)
    raw = re.sub(r"_+", "_", raw).strip("_")
    return (raw or fallback)[:80]


def is_focus_file_path(path: str | None) -> bool:
    normalized = (path or "").strip().replace("\\", "/").strip("/")
    return normalized.lower() in {"focus.md", "agenda.md"}


def _serialize_focus_item(item: AgentFocusItemRecord) -> SerializedFocusItem:
    return {
        "id": str(item.id),
        "agent_id": str(item.agent_id),
        "key": item.key,
        "title": item.title,
        "description": item.description or item.key,
        "status": item.status,
        "kind": item.kind,
        "source": item.source,
        "metadata": item.item_metadata or {},
        "sort_order": item.sort_order,
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


async def list_focus_items(agent_id: uuid.UUID, *, include_completed: bool = True) -> list[SerializedFocusItem]:
    items = await agent_focus_item_dao.list_for_agent(agent_id, include_completed=include_completed)
    return [_serialize_focus_item(item) for item in items]


async def upsert_focus_item(
    agent_id: uuid.UUID,
    *,
    key: str | None,
    title: str | None = None,
    description: str,
    status: FocusStatus = FocusStatus.IN_PROGRESS,
    kind: FocusKind = FocusKind.NORMAL,
    source: str = "user",
    metadata: JsonObject | None = None,
) -> SerializedFocusItem:
    desc = (description or "").strip()
    item_key = (key or "").strip() or slugify_focus_key(desc)
    item_key = item_key[:200]
    if kind == "system" and not item_key.startswith("system:"):
        item_key = f"system:{item_key}"[:200]

    item = await agent_focus_item_dao.get_by_key(agent_id, item_key)
    if item:
        updates: dict[str, str | datetime | JsonObject | None] = {
            "description": desc or item.description or item_key,
            "status": status,
            "kind": kind,
            "source": source or item.source or "user",
            "completed_at": datetime.now(UTC) if status == "completed" else None,
        }
        if title is not None:
            updates["title"] = title
        if metadata is not None and metadata:
            updates["item_metadata"] = {**(item.item_metadata or {}), **metadata}
        updated = await agent_focus_item_dao.update(db_obj=item, obj_in=updates)
        return _serialize_focus_item(updated)

    max_order = await agent_focus_item_dao.max_sort_order(agent_id)
    created = await agent_focus_item_dao.create(
        obj_in={
            "agent_id": agent_id,
            "key": item_key,
            "title": title,
            "description": desc or item_key,
            "status": status,
            "kind": kind,
            "source": source or "user",
            "item_metadata": metadata or {},
            "sort_order": max_order + 1,
            "completed_at": datetime.now(UTC) if status == "completed" else None,
        }
    )
    return _serialize_focus_item(created)


async def ensure_focus_item(
    agent_id: uuid.UUID,
    *,
    focus_ref: str | None,
    title: str | None = None,
    description: str,
    system: bool = False,
    source: str = "trigger",
) -> str:
    item = await upsert_focus_item(
        agent_id,
        key=focus_ref,
        title=title,
        description=description,
        status=FocusStatus.IN_PROGRESS,
        kind=FocusKind.SYSTEM if system else FocusKind.NORMAL,
        source=source,
    )
    return item["key"]


async def complete_focus_item(agent_id: uuid.UUID, *, key: str) -> SerializedFocusItem | None:
    item = await agent_focus_item_dao.get_by_key(agent_id, key)
    if not item:
        return None
    updated = await agent_focus_item_dao.update(
        db_obj=item,
        obj_in={"status": FocusStatus.COMPLETED, "completed_at": datetime.now(UTC)},
    )
    return _serialize_focus_item(updated)


async def render_focus_context(agent_id: uuid.UUID) -> str:
    items = await list_focus_items(agent_id, include_completed=True)
    active = [i for i in items if i["status"] != "completed" and i["kind"] != "system"]
    system = [i for i in items if i["status"] != "completed" and i["kind"] == "system"]
    completed = [i for i in items if i["status"] == "completed"][:12]
    lines: list[str] = []
    if active:
        lines.append("In Progress")
        for i in active:
            if i.get("title") not in (None, ""):
                lines.append(f"- {i['title']} ({i['key']}): {i['description']}")
            else:
                lines.append(f"- {i['key']}: {i['description']}")
    if system:
        if lines:
            lines.append("")
        lines.append("System Focus")
        for i in system:
            if i.get("title") not in (None, ""):
                lines.append(f"- {i['title']} ({i['key']}): {i['description']}")
            else:
                lines.append(f"- {i['key']}: {i['description']}")
    if completed:
        if lines:
            lines.append("")
        lines.append("Recently Completed")
        for i in completed:
            if i.get("title") not in (None, ""):
                lines.append(f"- {i['title']} ({i['key']}): {i['description']}")
            else:
                lines.append(f"- {i['key']}: {i['description']}")
    return "\n".join(lines)
