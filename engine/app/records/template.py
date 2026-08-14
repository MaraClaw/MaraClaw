"""Agent template records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.json_types import mapping_from_row, str_list_from_row


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
    def from_row(cls, row: dict[str, Any]) -> AgentTemplateRecord:
        default_skills = str_list_from_row(row.get("default_skills") or [])
        default_mcp_servers = str_list_from_row(row.get("default_mcp_servers") or [])
        default_autonomy_policy = mapping_from_row(row.get("default_autonomy_policy") or {})
        capability_bullets = str_list_from_row(row.get("capability_bullets") or [])
        return cls(
            id=row["id"],
            name=row["name"],
            description=row.get("description") or "",
            icon=row.get("icon") or "🤖",
            category=row.get("category") or "general",
            soul_template=row.get("soul_template") or "",
            default_skills=default_skills,
            default_mcp_servers=default_mcp_servers,
            default_autonomy_policy=default_autonomy_policy,
            capability_bullets=capability_bullets,
            bootstrap_content=row.get("bootstrap_content"),
            is_builtin=bool(row.get("is_builtin", False)),
            created_by=row.get("created_by"),
            created_at=row.get("created_at"),
        )
