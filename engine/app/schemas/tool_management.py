"""Requests for company MCP imports and field-level configuration edits."""

from typing import ClassVar, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.json_types import JsonObject


class ConfigOperations(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    set: JsonObject = Field(default_factory=dict)
    unset: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def disjoint_fields(self) -> Self:
        if len(self.unset) != len(set(self.unset)) or self.set.keys() & set(self.unset):
            raise ValueError("Configuration operations must be disjoint and unique")
        if any(not key or "." in key for key in [*self.set, *self.unset]):
            raise ValueError("Configuration operations require top-level field names")
        return self


class CompanyConfigOperations(ConfigOperations):
    tenant_id: UUID | None = None


class SavedToolReference(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    tool_id: UUID
    agent_id: UUID | None = None
    tenant_id: UUID | None = None

    @model_validator(mode="after")
    def single_scope(self) -> Self:
        if self.agent_id is not None and self.tenant_id is not None:
            raise ValueError("Agent references cannot override the company")
        return self


class SavedToolProbe(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    saved: SavedToolReference
    operations: ConfigOperations = Field(default_factory=ConfigOperations)


class MCPDiscoveredTool(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True)

    name: str = Field(min_length=1)
    description: str = ""
    input_schema: JsonObject = Field(default_factory=dict, alias="inputSchema")

    @model_validator(mode="after")
    def object_input(self) -> Self:
        if self.input_schema.get("type", "object") != "object":
            raise ValueError("MCP tool input schemas must describe an object")
        return self


class MCPCatalogDiscovery(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    server_url: str = Field(min_length=1, max_length=500)
    api_key: str | None = Field(default=None, repr=False)
    tenant_id: UUID | None = None


class MCPCatalogImport(MCPCatalogDiscovery):
    server_name: str = Field(min_length=1, max_length=98)
    name_prefix: str = Field(min_length=1, max_length=98, pattern=r"^[a-zA-Z0-9_-]+$")
    tools: list[MCPDiscoveredTool] = Field(min_length=1)

    @model_validator(mode="after")
    def local_names(self) -> Self:
        names = [f"{self.name_prefix}_{tool.name}" for tool in self.tools]
        if len(names) != len(set(names)) or any(len(name) > 100 for name in names):
            raise ValueError("Selected tools require unique local names of at most 100 characters")
        return self
