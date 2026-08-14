"""Tool and agent-tool assignment records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.json_types import datetime_from_row, mapping_from_row, str_from_row, uuid_from_row, uuid_from_row_opt


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
    def from_row(cls, row: Mapping[str, object]) -> ToolRecord:
        parameters_schema = mapping_from_row(row.get("parameters_schema") or {})
        config = mapping_from_row(row.get("config") or {})
        config_schema = mapping_from_row(row.get("config_schema") or {})
        return cls(
            id=uuid_from_row(row["id"]),
            name=str_from_row(row["name"]),
            display_name=str_from_row(row.get("display_name")),
            description=str_from_row(row.get("description")),
            type=str_from_row(row.get("type"), "builtin") or "builtin",
            category=str_from_row(row.get("category"), "general") or "general",
            icon=str_from_row(row.get("icon"), "🔧") or "🔧",
            parameters_schema=parameters_schema,
            config=config,
            config_schema=config_schema,
            mcp_server_url=str_from_row(row["mcp_server_url"]) or None,
            mcp_server_name=str_from_row(row["mcp_server_name"]) or None,
            mcp_tool_name=str_from_row(row["mcp_tool_name"]) or None,
            enabled=bool(row.get("enabled", True)),
            is_default=bool(row.get("is_default", False)),
            source=str_from_row(row.get("source"), "builtin") or "builtin",
            tenant_id=uuid_from_row_opt(row.get("tenant_id")),
            created_at=datetime_from_row(row.get("created_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
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
    def from_row(cls, row: Mapping[str, object]) -> AgentToolRecord:
        config = mapping_from_row(row.get("config") or {})
        return cls(
            id=uuid_from_row(row["id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            tool_id=uuid_from_row(row["tool_id"]),
            enabled=bool(row.get("enabled", True)),
            config=config,
            source=str_from_row(row.get("source"), "system") or "system",
            installed_by_agent_id=uuid_from_row_opt(row.get("installed_by_agent_id")),
            created_at=datetime_from_row(row.get("created_at")),
        )
