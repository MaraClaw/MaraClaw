"""Tool and agent-tool assignment records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


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
    parameters_schema: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    config_schema: dict[str, Any] = field(default_factory=dict)
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
        parameters_schema = row.get("parameters_schema") or {}
        config = row.get("config") or {}
        config_schema = row.get("config_schema") or {}
        if not isinstance(parameters_schema, dict):
            parameters_schema = dict(parameters_schema)
        if not isinstance(config, dict):
            config = dict(config)
        if not isinstance(config_schema, dict):
            config_schema = dict(config_schema)
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
    config: dict[str, Any] = field(default_factory=dict)
    source: str = "system"
    installed_by_agent_id: UUID | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AgentToolRecord:
        config = row.get("config") or {}
        if not isinstance(config, dict):
            config = dict(config)
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
