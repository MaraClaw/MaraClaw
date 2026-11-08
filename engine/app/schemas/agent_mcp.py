import re
from typing import Annotated, ClassVar, Literal, Self

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

type MCPTransport = Literal["streamable-http", "sse", "stdio"]
type MCPScalar = str | int | float | bool
type MCPServerName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
type MCPTimeout = Annotated[float, Field(gt=0, allow_inf_nan=False)]

_HTTP_URL = TypeAdapter(AnyHttpUrl)
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class MCPServerFields(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)

    enabled: bool = True
    url: str | None = None
    headers: dict[str, MCPScalar] = Field(default_factory=dict)
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, MCPScalar] = Field(default_factory=dict)
    cwd: str | None = None
    connection_timeout_ms: MCPTimeout | None = None
    request_timeout_ms: MCPTimeout | None = None

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.strip() or any(ord(char) < 32 for char in value):
                raise ValueError("Expected an HTTP(S) URL")
            _ = _HTTP_URL.validate_python(value)
        return value

    @field_validator("command", "cwd")
    @classmethod
    def valid_guest_string(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\0" in value):
            raise ValueError("Expected a nonblank guest string without NUL")
        return value

    @field_validator("args")
    @classmethod
    def valid_args(cls, value: list[str]) -> list[str]:
        if any("\0" in arg for arg in value):
            raise ValueError("Arguments cannot contain NUL")
        return value

    @field_validator("headers")
    @classmethod
    def valid_headers(cls, value: dict[str, MCPScalar]) -> dict[str, MCPScalar]:
        if any(not _HEADER_NAME.fullmatch(key) for key in value):
            raise ValueError("Invalid header name")
        if len({key.lower() for key in value}) != len(value):
            raise ValueError("Duplicate header name")
        if any(isinstance(item, str) and any(char in item for char in "\r\n\0") for item in value.values()):
            raise ValueError("Invalid header value")
        return value

    @field_validator("env")
    @classmethod
    def valid_env(cls, value: dict[str, MCPScalar]) -> dict[str, MCPScalar]:
        if any(not key or "=" in key or "\0" in key for key in value):
            raise ValueError("Invalid environment key")
        if any(isinstance(item, str) and "\0" in item for item in value.values()):
            raise ValueError("Invalid environment value")
        return value


class MCPServerDefinition(MCPServerFields):
    transport: MCPTransport

    def check_transport_fields(self, fields: set[str]) -> None:
        match self.transport:
            case "stdio":
                forbidden = {"url", "headers", "removed_headers"}
            case "streamable-http" | "sse":
                forbidden = {"command", "args", "env", "cwd", "removed_env"}
        if fields & forbidden:
            raise ValueError("Fields do not match the server transport")

    @model_validator(mode="after")
    def valid_transport(self) -> Self:
        match self.transport:
            case "stdio":
                if self.command is None or self.url is not None or self.headers:
                    raise ValueError("Stdio requires command and excludes URL/headers")
            case "streamable-http" | "sse":
                if self.url is None or self.command is not None or self.args or self.env or self.cwd is not None:
                    raise ValueError("Remote transport requires URL and excludes stdio fields")
        return self


class MCPServerCreate(MCPServerDefinition):
    name: MCPServerName

    @model_validator(mode="after")
    def valid_optional_fields(self) -> Self:
        self.check_transport_fields(self.model_fields_set)
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Create fields cannot be null")
        return self


class MCPServerPatch(MCPServerFields):
    removed_headers: list[str] = Field(default_factory=list)
    removed_env: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_patch(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("At least one configuration field is required")
        nullable = {"cwd", "connection_timeout_ms", "request_timeout_ms"}
        if any(getattr(self, key) is None for key in self.model_fields_set - nullable):
            raise ValueError("Only cwd and timeouts may be null")
        if {key.lower() for key in self.headers} & {key.lower() for key in self.removed_headers}:
            raise ValueError("Cannot set and remove the same header")
        if set(self.env) & set(self.removed_env):
            raise ValueError("Cannot set and remove the same environment key")
        return self


class MCPServerSummary(BaseModel):
    name: str
    transport: MCPTransport
    enabled: bool
    url_configured: bool
    command_configured: bool
    args_count: int
    header_names: list[str]
    env_names: list[str]
    connection_timeout_ms: float | None
    request_timeout_ms: float | None


class MCPServerInventory(BaseModel):
    servers: list[MCPServerSummary]
