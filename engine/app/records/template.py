"""Agent template records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.json_types import (
    datetime_from_row,
    mapping_from_row,
    str_from_row,
    str_list_from_row,
    uuid_from_row,
    uuid_from_row_opt,
)


@dataclass(slots=True)
class AgentTemplateRecord:
    """Digital employee template for quick creation."""

    id: UUID
    name: str
    description: str = ""
    icon: str = "🤖"
    category: str = "general"
    soul_template: str = ""
    default_skills: list[str] = field(default_factory=list[str])
    default_mcp_servers: list[str] = field(default_factory=list[str])
    default_autonomy_policy: dict[str, Any] = field(default_factory=dict[str, Any])
    capability_bullets: list[str] = field(default_factory=list[str])
    bootstrap_content: str | None = None
    is_builtin: bool = False
    created_by: UUID | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AgentTemplateRecord:
        default_skills = str_list_from_row(row.get("default_skills") or [])
        default_mcp_servers = str_list_from_row(row.get("default_mcp_servers") or [])
        default_autonomy_policy = mapping_from_row(row.get("default_autonomy_policy") or {})
        capability_bullets = str_list_from_row(row.get("capability_bullets") or [])
        return cls(
            id=uuid_from_row(row["id"]),
            name=str_from_row(row["name"]),
            description=str_from_row(row.get("description")),
            icon=str_from_row(row.get("icon"), "🤖") or "🤖",
            category=str_from_row(row.get("category"), "general") or "general",
            soul_template=str_from_row(row.get("soul_template")),
            default_skills=default_skills,
            default_mcp_servers=default_mcp_servers,
            default_autonomy_policy=default_autonomy_policy,
            capability_bullets=capability_bullets,
            bootstrap_content=str_from_row(row["bootstrap_content"]) or None,
            is_builtin=bool(row.get("is_builtin", False)),
            created_by=uuid_from_row_opt(row.get("created_by")),
            created_at=datetime_from_row(row.get("created_at")),
        )
