"""Skill registry records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row, uuid_from_row_opt


@dataclass(slots=True)
class SkillFileRecord:
    """A file within a skill folder."""

    id: UUID
    skill_id: UUID
    path: str
    content: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> SkillFileRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            skill_id=uuid_from_row(row["skill_id"]),
            path=str_from_row(row["path"]),
            content=str_from_row(row.get("content")),
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
    files: list[SkillFileRecord] = field(default_factory=list[SkillFileRecord])

    @classmethod
    def from_row(cls, row: Mapping[str, object], *, files: list[SkillFileRecord] | None = None) -> SkillRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            name=str_from_row(row["name"]),
            folder_name=str_from_row(row["folder_name"]),
            tenant_id=uuid_from_row_opt(row.get("tenant_id")),
            description=str_from_row(row.get("description")),
            category=str_from_row(row.get("category"), "general") or "general",
            icon=str_from_row(row.get("icon"), "📋") or "📋",
            is_builtin=bool(row.get("is_builtin", False)),
            is_default=bool(row.get("is_default", False)),
            created_at=datetime_from_row(row.get("created_at")),
            files=list(files or []),
        )
