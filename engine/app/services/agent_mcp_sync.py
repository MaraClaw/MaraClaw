"""Database-authoritative native MCP publication. Callers hold the exclusive workspace flock."""

import json
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from anyio import to_thread
from fastapi import HTTPException
from pydantic import ConfigDict, TypeAdapter, ValidationError

from app.config import get_settings
from app.core.json_types import JsonObject, is_json_object
from app.core.security import decrypt_data, encrypt_data
from app.dao.agent_mcp_dao import agent_mcp_server_dao
from app.records.agent import AgentRecord
from app.records.agent_mcp import AgentMCPServerRecord
from app.services import agent_mcp_config as native

if TYPE_CHECKING:
    from app.services.agent_manager import AgentManager

_DEFINITION = TypeAdapter(JsonObject, config=ConfigDict(allow_inf_nan=False))


def encrypted_record(agent_id: UUID, name: str, definition: JsonObject | None) -> AgentMCPServerRecord:
    ciphertext = None
    if definition is not None:
        ciphertext = encrypt_data(json.dumps(definition, allow_nan=False), get_settings().SECRET_KEY)
    return AgentMCPServerRecord(agent_id, name, ciphertext)


async def desired_servers(agent_id: UUID) -> dict[str, JsonObject | None]:
    records = await agent_mcp_server_dao.list_for_agent(agent_id)
    try:
        desired: dict[str, JsonObject | None] = {}
        for row in records:
            entry = None
            if row.definition_ciphertext is not None:
                entry = _DEFINITION.validate_json(decrypt_data(row.definition_ciphertext, get_settings().SECRET_KEY))
                _ = native.parse_server(entry)
            desired[row.name] = entry
        return desired
    except ValueError, ValidationError:
        raise HTTPException(409, detail="mcp_saved_config_invalid") from None


def reconcile_config(config: JsonObject, desired: dict[str, JsonObject | None]) -> JsonObject:
    servers = native.config_servers(config)
    for name, definition in desired.items():
        if definition is None:
            servers.pop(name, None)
        else:
            servers[name] = definition
    mcp = config.get("mcp", {})
    if not is_json_object(mcp):
        raise HTTPException(409, detail="mcp_config_invalid")
    return {**config, "mcp": {**mcp, "servers": dict(servers)}}


async def sync_mcp_config(agent_id: UUID, path: Path, *, running: bool = False) -> bool:
    desired = await desired_servers(agent_id)
    if not await to_thread.run_sync(path.exists):
        return False
    if not desired:
        return True
    config = await to_thread.run_sync(native.read_config, path)
    updated = reconcile_config(config, desired)
    if updated != config and running:
        native.require_hot_reload(config)
    try:
        mode = (await to_thread.run_sync(path.stat)).st_mode & 0o777
    except OSError:
        raise HTTPException(503, detail="mcp_config_io_error") from None
    if updated != config or mode != 0o600:
        await to_thread.run_sync(native.write_config, path, updated)
    return True


async def restore_existing_guest(manager: AgentManager, agent: AgentRecord, *, running: bool = False) -> None:
    """Resume/running recovery must reconcile even when model generation would be skipped."""
    path = manager._agent_dir(agent.id) / "openclaw.json"
    desired = await desired_servers(agent.id)
    if not desired:
        return
    if not await to_thread.run_sync(path.exists):
        from app.dao.llm_dao import llm_model_dao
        from app.services.enterprise_llm import owned_model_or_none

        ids = [item for item in (agent.primary_model_id, agent.fallback_model_id) if item]
        models = {row.id: row for row in await llm_model_dao.get_many(ids)} if ids else {}
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = manager.write_guest_config(
            agent,
            primary=owned_model_or_none(
                models.get(agent.primary_model_id) if agent.primary_model_id else None, agent.tenant_id
            ),
            fallback=owned_model_or_none(
                models.get(agent.fallback_model_id) if agent.fallback_model_id else None, agent.tenant_id
            ),
        )
    _ = await sync_mcp_config(agent.id, path, running=running)
