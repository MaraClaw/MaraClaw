"""Tool and agent-tool assignment records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.json_types import mapping_from_row


@dataclass(slots=True)
class ToolRecord:
    """Platform tool catalog entry."""

    id: UUID
    name: str
    display_name: str = ""
    description: str = ""
    type: str = "builtin"
    category: str = "general"
    icon: str = "🔧"
    parameters_schema: dict[str, Any] = field(default_factory=dict[str, Any])
    config: dict[str, Any] = field(default_factory=dict[str, Any])
    config_schema: dict[str, Any] = field(default_factory=dict[str, Any])
    mcp_server_url: str | None = None
    mcp_server_name: str | None = None
    mcp_tool_name: str | None = None
    enabled: bool = True
    is_default: bool = False
    source: str = "builtin"
    tenant_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ToolRecord:
        parameters_schema = mapping_from_row(row.get("parameters_schema") or {})
        config = mapping_from_row(row.get("config") or {})
        config_schema = mapping_from_row(row.get("config_schema") or {})
        return cls(
            id=row["id"],
            name=row["name"],
            display_name=row.get("display_name") or "",
            description=row.get("description") or "",
            type=row.get("type") or "builtin",
            category=row.get("category") or "general",
            icon=row.get("icon") or "🔧",
            parameters_schema=parameters_schema,
            config=config,
            config_schema=config_schema,
            mcp_server_url=row.get("mcp_server_url"),
            mcp_server_name=row.get("mcp_server_name"),
            mcp_tool_name=row.get("mcp_tool_name"),
            enabled=bool(row.get("enabled", True)),
            is_default=bool(row.get("is_default", False)),
            source=row.get("source") or "builtin",
            tenant_id=row.get("tenant_id"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )


@dataclass(slots=True)
class AgentToolRecord:
    """Junction: which tools are enabled for which agent."""

    id: UUID
    agent_id: UUID
    tool_id: UUID
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict[str, Any])
    source: str = "system"
    installed_by_agent_id: UUID | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AgentToolRecord:
        config = mapping_from_row(row.get("config") or {})
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            tool_id=row["tool_id"],
            enabled=bool(row.get("enabled", True)),
            config=config,
            source=row.get("source") or "system",
            installed_by_agent_id=row.get("installed_by_agent_id"),
            created_at=row.get("created_at"),
        )
