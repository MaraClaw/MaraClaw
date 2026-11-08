"""Native config parsing and private atomic publication; callers own the workspace lock."""

import json
import os
import tempfile
from pathlib import Path

from fastapi import HTTPException
from pydantic import ConfigDict, TypeAdapter, ValidationError

from app.core.json_types import JsonObject, is_json_object
from app.schemas.agent_mcp import MCPServerDefinition, MCPServerSummary

_CONFIG = TypeAdapter(JsonObject, config=ConfigDict(allow_inf_nan=False))
_TIMEOUT_KEYS = {"connection_timeout_ms": "connectionTimeoutMs", "request_timeout_ms": "requestTimeoutMs"}


def native_fields(fields: JsonObject) -> JsonObject:
    return {_TIMEOUT_KEYS.get(key, key): value for key, value in fields.items()}


def parse_server(server: JsonObject) -> MCPServerDefinition:
    fields = {
        key: server[native_key]
        for key in MCPServerDefinition.model_fields
        if (native_key := _TIMEOUT_KEYS.get(key, key)) in server
    }
    if "transport" not in fields:
        fields["transport"] = "stdio" if bool(server.get("command")) else "sse"
    return MCPServerDefinition.model_validate(fields)


def server_summary(name: str, server: JsonObject) -> MCPServerSummary:
    definition = parse_server(server)
    return MCPServerSummary(
        name=name,
        transport=definition.transport,
        enabled=definition.enabled,
        url_configured=definition.url is not None,
        command_configured=definition.command is not None,
        args_count=len(definition.args),
        header_names=sorted(definition.headers),
        env_names=sorted(definition.env),
        connection_timeout_ms=definition.connection_timeout_ms,
        request_timeout_ms=definition.request_timeout_ms,
    )


def read_config(path: Path) -> JsonObject:
    try:
        if path.is_symlink():
            raise HTTPException(409, detail="mcp_config_invalid")
        config = _CONFIG.validate_json(path.read_bytes())
        mcp = config.get("mcp", {})
        if not is_json_object(mcp) or not is_json_object(servers := mcp.get("servers", {})):
            raise HTTPException(409, detail="mcp_config_invalid")
        for name, entry in servers.items():
            if name == "__proto__" or not is_json_object(entry):
                raise HTTPException(409, detail="mcp_config_invalid")
            _ = parse_server(entry)
        return config
    except FileNotFoundError:
        raise HTTPException(409, detail="mcp_config_unavailable") from None
    except ValidationError:
        raise HTTPException(409, detail="mcp_config_invalid") from None
    except OSError:
        raise HTTPException(503, detail="mcp_config_io_error") from None


def config_servers(config: JsonObject) -> dict[str, JsonObject]:
    mcp = config.get("mcp", {})
    if not is_json_object(mcp) or not is_json_object(servers := mcp.get("servers", {})):
        raise HTTPException(409, detail="mcp_config_invalid")
    return {name: entry for name, entry in servers.items() if is_json_object(entry)}


def write_config(path: Path, config: JsonObject) -> None:
    try:
        owner = path.stat()
        with tempfile.TemporaryDirectory(dir=path.parent, prefix=".mcp-") as temporary:
            staged = Path(temporary) / "openclaw.json"
            with staged.open("x", encoding="utf-8") as out:
                # Preserve guest ownership: root-owned 0600 would make the node guest unable to reload.
                os.fchown(out.fileno(), owner.st_uid, owner.st_gid)
                os.fchmod(out.fileno(), 0o600)
                json.dump(config, out, ensure_ascii=False, indent=2, allow_nan=False)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            _ = staged.replace(path)
    except OSError:
        raise HTTPException(503, detail="mcp_config_io_error") from None


def require_hot_reload(config: JsonObject) -> None:
    gateway = config.get("gateway", {})
    if not is_json_object(gateway) or not is_json_object(reload := gateway.get("reload", {})):
        raise HTTPException(409, detail="mcp_config_invalid")
    if reload.get("mode", "hybrid") not in ("hybrid", "hot"):
        raise HTTPException(409, detail="mcp_hot_reload_required")
