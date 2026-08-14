"""Structured Focus service.

Focus is stored in the database. Legacy focus.md parsing exists only for a
one-time import path; runtime reads and writes must use this service.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from app.config import get_settings
from app.core.json_types import JsonObject
from app.dao.focus_dao import agent_focus_item_dao
from app.records.focus import AgentFocusItemRecord

_settings = get_settings()
WORKSPACE_ROOT = Path(_settings.AGENT_DATA_DIR)

ACTIVE_SECTION = "进行中"
SYSTEM_SECTION = "系统 Focus"
COMPLETED_SECTION = "已完成"
SECTION_TITLES = (ACTIVE_SECTION, SYSTEM_SECTION, COMPLETED_SECTION)
FOCUS_LINE_RE = re.compile(r"^\s*-\s*\[([ xX/])\]\s*(.+?)\s*$")
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
VALID_STATUSES = {"in_progress", "completed"}
VALID_KINDS = {"normal", "system"}


@dataclass
class FocusItem:
    key: str
    description: str
    marker: str
    section: str


class SerializedFocusItem(TypedDict):
    id: str
    agent_id: str
    key: str
    title: str | None
    description: str
    status: str
    kind: str
    source: str
    metadata: JsonObject
    sort_order: int
    completed_at: str | None
    created_at: str | None
    updated_at: str | None


class FocusMigrationRow(TypedDict):
    agent_id: uuid.UUID
    key: str
    description: str
    status: str
    kind: str
    source: str
    sort_order: int
    completed_at: datetime | None
    item_metadata: JsonObject


def slugify_focus_key(value: str, *, fallback: str = "focus") -> str:
    """Create a stable ASCII-ish key usable as focus_ref."""
    raw = (value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", raw)
    raw = re.sub(r"_+", "_", raw).strip("_")
    return (raw or fallback)[:80]


def is_focus_file_path(path: str | None) -> bool:
    normalized = (path or "").strip().replace("\\", "/").strip("/")
    return normalized.lower() in {"focus.md", "agenda.md"}


def _focus_path(agent_id: uuid.UUID) -> Path:
    return WORKSPACE_ROOT / str(agent_id) / "focus.md"


def _format_empty_focus() -> str:
    return f"# Focus\n\n## {ACTIVE_SECTION}\n\n## {SYSTEM_SECTION}\n\n## {COMPLETED_SECTION}\n"


def _split_focus_key_description(full_text: str) -> tuple[str, str]:
    system_match = re.match(r"^(system:[^:]+)\s*:\s*(.*)$", full_text)
    if system_match:
        return system_match.group(1).strip(), system_match.group(2).strip()
    key, sep, desc = full_text.partition(":")
    return key.strip(), desc.strip() if sep else ""


def _section_for_focus_line(line: str, fallback: str) -> str:
    match = FOCUS_LINE_RE.match(line)
    if not match:
        return fallback
    marker = match.group(1).lower()
    full_text = match.group(2).strip()
    key, _ = _split_focus_key_description(full_text)
    if marker == "x":
        return COMPLETED_SECTION
    if key.startswith("system:"):
        return SYSTEM_SECTION
    return fallback if fallback in SECTION_TITLES else ACTIVE_SECTION


def _normalize_sectioned_focus(text: str) -> str:
    lines = text.splitlines()
    heading = "# Focus"
    prelude: list[str] = []
    sections: dict[str, list[str]] = {title: [] for title in SECTION_TITLES}
    current_section: str | None = None

    for raw_line in lines:
        line = raw_line.rstrip()
        if re.match(r"^#\s+Focus\s*$", line, re.I):
            heading = line
            continue

        section_match = SECTION_RE.match(line)
        if section_match and section_match.group(1).strip() in SECTION_TITLES:
            current_section = section_match.group(1).strip()
            continue

        target_section = current_section
        if FOCUS_LINE_RE.match(line):
            target_section = _section_for_focus_line(line, current_section or ACTIVE_SECTION)

        if target_section in SECTION_TITLES:
            sections[target_section].append(line)
        elif line.strip():
            prelude.append(line)

    parts: list[str] = [heading, ""]
    if prelude:
        parts.extend(prelude)
        parts.append("")

    for title in SECTION_TITLES:
        parts.append(f"## {title}")
        body = [line for line in sections[title] if line.strip()]
        if body:
            parts.append("")
            parts.extend(body)
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def ensure_focus_sections(content: str) -> str:
    """Normalize legacy focus.md content for one-time imports."""
    text = content.strip("\ufeff")
    if not text.strip():
        return _format_empty_focus()

    has_known_section = any(re.search(rf"^##\s+{re.escape(title)}\s*$", text, re.M) for title in SECTION_TITLES)
    if not has_known_section:
        heading = "# Focus"
        body = text
        first = text.splitlines()[0].strip() if text.splitlines() else ""
        if re.match(r"^#\s+Focus\s*$", first, re.I):
            lines = text.splitlines()
            heading = lines[0]
            body = "\n".join(lines[1:]).strip()
        return _normalize_sectioned_focus(
            (f"{heading}\n\n## {ACTIVE_SECTION}\n\n{body}\n\n## {SYSTEM_SECTION}\n\n## {COMPLETED_SECTION}\n").strip()
            + "\n"
        )

    if not re.search(r"^#\s+Focus\s*$", text, re.M):
        text = "# Focus\n\n" + text.strip()

    for title in SECTION_TITLES:
        if not re.search(rf"^##\s+{re.escape(title)}\s*$", text, re.M):
            text = text.rstrip() + f"\n\n## {title}\n"
    return _normalize_sectioned_focus(text.rstrip() + "\n")


def parse_focus_items(content: str) -> list[FocusItem]:
    section = ACTIVE_SECTION
    items: list[FocusItem] = []
    for line in content.splitlines():
        heading = SECTION_RE.match(line)
        if heading:
            title = heading.group(1).strip()
            if title in SECTION_TITLES:
                section = title
            continue
        match = FOCUS_LINE_RE.match(line)
        if not match:
            continue
        marker = match.group(1).lower()
        key, desc = _split_focus_key_description(match.group(2).strip())
        if not key:
            continue
        items.append(FocusItem(key=key, description=desc or key, marker=marker, section=section))
    return items


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


async def migrate_legacy_focus_file(agent_id: uuid.UUID, db: object | None = None) -> int:
    """Import legacy focus.md once when the DB has no focus rows.

    ``db`` is accepted for dual-stack call-site compatibility and ignored;
    persistence goes through the focus DAO / connection_ctx.
    """
    existing_count = await agent_focus_item_dao.count_for_agent(agent_id)
    if existing_count:
        return 0

    path = _focus_path(agent_id)
    if not path.exists():
        return 0

    try:
        content = ensure_focus_sections(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return 0

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for order, legacy in enumerate(parse_focus_items(content)):
        key = legacy.key[:200]
        if not key or key in seen:
            continue
        seen.add(key)
        status = "completed" if legacy.marker == "x" or legacy.section == COMPLETED_SECTION else "in_progress"
        kind = "system" if legacy.section == SYSTEM_SECTION or key.startswith("system:") else "normal"
        rows.append(
            {
                "agent_id": agent_id,
                "key": key,
                "description": legacy.description or key,
                "status": status,
                "kind": kind,
                "source": "migration",
                "sort_order": order,
                "completed_at": datetime.now(UTC) if status == "completed" else None,
                "item_metadata": {"legacy_section": legacy.section, "legacy_marker": legacy.marker},
            }
        )
    if not rows:
        return 0
    return await agent_focus_item_dao.bulk_insert_ignore(list(rows))


async def list_focus_items(
    agent_id: uuid.UUID, *, include_completed: bool = True, db: object | None = None
) -> list[SerializedFocusItem]:
    _ = await migrate_legacy_focus_file(agent_id, db=db)
    items = await agent_focus_item_dao.list_for_agent(agent_id, include_completed=include_completed)
    return [_serialize_focus_item(item) for item in items]


async def upsert_focus_item(
    agent_id: uuid.UUID,
    *,
    key: str | None,
    title: str | None = None,
    description: str,
    status: str = "in_progress",
    kind: str = "normal",
    source: str = "user",
    metadata: JsonObject | None = None,
    db: object | None = None,
) -> SerializedFocusItem:
    _ = await migrate_legacy_focus_file(agent_id, db=db)
    desc = (description or "").strip()
    item_key = (key or "").strip() or slugify_focus_key(desc)
    item_key = item_key[:200]
    if kind == "system" and not item_key.startswith("system:"):
        item_key = f"system:{item_key}"[:200]
    if status not in VALID_STATUSES:
        status = "in_progress"
    if kind not in VALID_KINDS:
        kind = "normal"

    item = await agent_focus_item_dao.get_by_key(agent_id, item_key)
    if item:
        updates: dict[str, Any] = {
            "description": desc or item.description or item_key,
            "status": status,
            "kind": kind,
            "source": source or item.source or "user",
            "completed_at": datetime.now(UTC) if status == "completed" else None,
        }
        if title is not None:
            updates["title"] = title
        if metadata:
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
    db: object | None = None,
) -> str:
    item = await upsert_focus_item(
        agent_id,
        key=focus_ref,
        title=title,
        description=description,
        status="in_progress",
        kind="system" if system else "normal",
        source=source,
        db=db,
    )
    return item["key"]


async def complete_focus_item(agent_id: uuid.UUID, *, key: str) -> SerializedFocusItem | None:
    _ = await migrate_legacy_focus_file(agent_id)
    item = await agent_focus_item_dao.get_by_key(agent_id, key)
    if not item:
        return None
    updated = await agent_focus_item_dao.update(
        db_obj=item,
        obj_in={"status": "completed", "completed_at": datetime.now(UTC)},
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
            if i.get("title"):
                lines.append(f"- {i['title']} ({i['key']}): {i['description']}")
            else:
                lines.append(f"- {i['key']}: {i['description']}")
    if system:
        if lines:
            lines.append("")
        lines.append("System Focus")
        for i in system:
            if i.get("title"):
                lines.append(f"- {i['title']} ({i['key']}): {i['description']}")
            else:
                lines.append(f"- {i['key']}: {i['description']}")
    if completed:
        if lines:
            lines.append("")
        lines.append("Recently Completed")
        for i in completed:
            if i.get("title"):
                lines.append(f"- {i['title']} ({i['key']}): {i['description']}")
            else:
                lines.append(f"- {i['key']}: {i['description']}")
    return "\n".join(lines)
