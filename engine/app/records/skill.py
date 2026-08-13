"""Skill registry records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class SkillFileRecord:
    """A file within a skill folder."""

    id: UUID
    skill_id: UUID
    path: str
    content: str = ""

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> SkillFileRecord:
        return cls(
            id=row["id"],
            skill_id=row["skill_id"],
            path=row["path"],
            content=row.get("content") or "",
        )


@dataclass(slots=True)
class SkillRecord:
    """Globally registered skill definition."""

    id: UUID
    name: str
    folder_name: str
    tenant_id: UUID | None = None
    description: str = ""
    category: str = "general"
    icon: str = "📋"
    is_builtin: bool = False
    is_default: bool = False
    created_at: datetime | None = None
    files: list[SkillFileRecord] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: dict[str, Any], *, files: list[SkillFileRecord] | None = None) -> SkillRecord:
        return cls(
            id=row["id"],
            name=row["name"],
            folder_name=row["folder_name"],
            tenant_id=row.get("tenant_id"),
            description=row.get("description") or "",
            category=row.get("category") or "general",
            icon=row.get("icon") or "📋",
            is_builtin=bool(row.get("is_builtin", False)),
            is_default=bool(row.get("is_default", False)),
            created_at=row.get("created_at"),
            files=list(files or []),
        )
