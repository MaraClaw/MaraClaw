"""Agent tools - unified file-based tools that give digital employees
access to their own structured workspace.

Design principle:  ONE set of file tools covers EVERYTHING.
The agent's workspace uses well-known paths:
  - soul.md             → personality definition
  - memory/memory.md    → long-term memory / notes
  - skills/             → skill definitions (markdown files)
  - workspace/          → general working files, reports, etc.

The agent reads/writes these files directly. No per-concept tools needed.
"""

from __future__ import annotations

import importlib
import multiprocessing as mp
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, TypeIs

if TYPE_CHECKING:
    from app.services.agentbay_client import AgentBayClient

from app.config import get_settings
from app.core.json_types import JsonObject, JsonValue
from app.core.tool_types import ToolConfigSchema, ToolDefinition
from app.services.agent_tools_definitions import (
    _always_core_tools,
    _channel_tools,
    _feishu_tools,
)
from app.services.storage import get_storage_backend, normalize_storage_key
from app.services.storage_runtime.base import StorageBackend, StorageEntry
from app.services.tool_runtime import (
    catalog as _tool_runtime_catalog,
    catalog_computer as _tool_runtime_catalog_computer,
    tool_config as _tool_runtime_tool_config,
)
from app.services.workspace_collaboration import normalize_workspace_path

from .agent_tool_exec import (
    _agent_tool_exec_a2a,  # noqa: F401
    _agent_tool_exec_agentbay,  # noqa: F401
    _agent_tool_exec_conversion,  # noqa: F401
    _agent_tool_exec_deploy,
    _agent_tool_exec_deploy_ops,
    _agent_tool_exec_feishu,  # noqa: F401
    _agent_tool_exec_feishu_approvals,
    _agent_tool_exec_feishu_bitable,
    _agent_tool_exec_feishu_calendar,
    _agent_tool_exec_feishu_client,
    _agent_tool_exec_feishu_contacts,
    _agent_tool_exec_feishu_docs,
    _agent_tool_exec_feishu_drive,
    _agent_tool_exec_feishu_markdown,
    _agent_tool_exec_okr_access,
    _agent_tool_exec_okr_read,
    _agent_tool_exec_okr_reports,
    _agent_tool_exec_okr_write,
    _agent_tool_exec_search,  # noqa: F401
    _agent_tool_exec_storage,  # noqa: F401
    _agent_tool_exec_triggers,  # noqa: F401
    resolve as _resolve_tool_handler,
)
from .agent_tool_exec._agent_tool_exec_triggers import (
    VALID_TRIGGER_TYPES as VALID_TRIGGER_TYPES,
    _handle_cancel_trigger as _handle_cancel_trigger,
    _handle_list_triggers as _handle_list_triggers,
    _handle_set_trigger as _handle_set_trigger,
    _handle_update_trigger as _handle_update_trigger,
)
from .agent_tool_exec.a2a_context import (
    A2AContext as A2AContext,
    _build_a2a_context as _build_a2a_context,
)
from .agent_tool_exec.a2a_handlers import (
    _a2a_handle_consult as _a2a_handle_consult,
    _a2a_handle_notify as _a2a_handle_notify,
    _a2a_handle_openclaw as _a2a_handle_openclaw,
    _a2a_handle_task_delegate as _a2a_handle_task_delegate,
)
from .agent_tool_exec.a2a_send import (
    _send_file_to_agent as _send_file_to_agent,
    _send_message_to_agent as _send_message_to_agent,
)
from .agent_tool_exec.a2a_sessions import (
    _ensure_a2a_session as _ensure_a2a_session,
    _resolve_a2a_target as _resolve_a2a_target,
)
from .agent_tool_exec.a2a_triggers import (
    _append_focus_item as _append_focus_item,
    _create_on_message_trigger as _create_on_message_trigger,
    _wake_agent_async as _wake_agent_async,
)
from .agent_tool_exec.registry import ToolArgumentMapping, ToolArguments, ToolArgumentValue, ToolOutputCallback
from .agent_tool_exec.workspace import (
    _execute_workspace_mutation as _execute_workspace_mutation,
    _run_with_temp_workspace as _run_with_temp_workspace,
)
from .agent_tool_exec.workspace_temp import (
    TEMP_WORKSPACE_DEFAULT_PATHS as TEMP_WORKSPACE_DEFAULT_PATHS,
    TOOL_MATERIALIZE_MAX_FILE_BYTES as TOOL_MATERIALIZE_MAX_FILE_BYTES,
    TOOL_MATERIALIZE_MAX_TOTAL_BYTES as TOOL_MATERIALIZE_MAX_TOTAL_BYTES,
    TempWorkspace as TempWorkspace,
    TempWorkspaceManifestEntry as TempWorkspaceManifestEntry,
    _collect_temp_workspace_files as _collect_temp_workspace_files,
    _is_enterprise_info_path as _is_enterprise_info_path,
    _materialize_storage_path_with_budget as _materialize_storage_path_with_budget,
    _materialize_storage_workspace as _materialize_storage_workspace,
    _prepare_temp_workspace as _prepare_temp_workspace,
    flush_temp_workspace as flush_temp_workspace,
)

resolve_tool_handler = _resolve_tool_handler


_settings = get_settings()
WORKSPACE_ROOT = Path(_settings.STORAGE_LOCAL_ROOT or _settings.AGENT_DATA_DIR)
MAX_EXEC_STDOUT_CAPTURE_BYTES = 1_000_000
MAX_EXEC_STDERR_CAPTURE_BYTES = 500_000

# ─── Tool Config Cache ──────────────────────────────────────────
_tool_config_cache = _tool_runtime_tool_config._tool_config_cache
_TOOL_CONFIG_CACHE_TTL_SECONDS = _tool_runtime_tool_config._TOOL_CONFIG_CACHE_TTL_SECONDS
SENSITIVE_FIELD_KEYS = _tool_runtime_tool_config.SENSITIVE_FIELD_KEYS

type ToolParameters = JsonObject


def _decrypt_sensitive_fields(config: JsonObject, config_schema: ToolConfigSchema | None = None) -> JsonObject:
    return _tool_runtime_tool_config.decrypt_sensitive_fields(
        config,
        config_schema,
        sensitive_field_keys=SENSITIVE_FIELD_KEYS,
    )


def _get_cached_tool_config(agent_id: uuid.UUID | None, tool_name: str) -> JsonObject | None:
    return _tool_runtime_tool_config.get_cached_tool_config(agent_id, tool_name, cache=_tool_config_cache)


def _set_cached_tool_config(agent_id: uuid.UUID | None, tool_name: str, config: JsonObject) -> None:
    _tool_runtime_tool_config.set_cached_tool_config(
        agent_id,
        tool_name,
        config,
        cache=_tool_config_cache,
        ttl_seconds=_TOOL_CONFIG_CACHE_TTL_SECONDS,
    )


async def _get_tool_config(agent_id: uuid.UUID | None, tool_name: str) -> JsonObject | None:
    dependencies = _tool_runtime_tool_config.ToolConfigDependencies(
        decrypt_sensitive_fields=_decrypt_sensitive_fields,
        get_cached_tool_config=_get_cached_tool_config,
        set_cached_tool_config=_set_cached_tool_config,
    )
    return await _tool_runtime_tool_config.get_tool_config(agent_id, tool_name, dependencies=dependencies)


async def _get_computer_tool_config(
    agent_id: uuid.UUID, tool_name: str
) -> _tool_runtime_catalog_computer.ComputerToolConfig | None:
    config = await _get_tool_config(agent_id, tool_name)
    if config is None:
        return None
    os_type = config.get("os_type")
    return {"os_type": os_type} if isinstance(os_type, str) else {}


# Static definitions live in app.services.agent_tools_definitions and are re-exported here.


async def _get_computer_os_type(agent_id: uuid.UUID) -> str:
    return await _tool_runtime_catalog_computer.get_computer_os_type(
        agent_id,
        get_tool_config=_get_computer_tool_config,
    )


def _patch_computer_tool_descriptions(tools: list[ToolDefinition], os_type: str) -> list[ToolDefinition]:
    return _tool_runtime_catalog_computer.patch_computer_tool_descriptions(tools, os_type)


async def _agent_has_feishu(agent_id: uuid.UUID) -> bool:
    return await _tool_runtime_catalog.agent_has_feishu(agent_id)


async def _agent_has_any_channel(agent_id: uuid.UUID) -> bool:
    return await _tool_runtime_catalog.agent_has_any_channel(agent_id)


setattr(_agent_has_feishu, "_uses_catalog_channel_presence", True)
setattr(_agent_has_any_channel, "_uses_catalog_channel_presence", True)


# ─── Dynamic Tool Loading from DB ──────────────────────────────


def _strip_a2a_msg_type(tools: list[ToolDefinition]) -> list[ToolDefinition]:
    return _tool_runtime_catalog.strip_a2a_msg_type(tools)


def _is_json_value(value: object) -> TypeIs[JsonValue]:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        items: list[object] = list(value)
        return all(_is_json_value(item) for item in items)
    return _is_json_object(value)


def _is_json_object(value: object) -> TypeIs[JsonObject]:
    if not isinstance(value, dict):
        return False
    mapping: dict[object, object] = dict(value)
    return all(isinstance(key, str) and _is_json_value(item) for key, item in mapping.items())


def _is_catalog_tool_definition(value: object) -> TypeIs[ToolDefinition]:
    if not _is_json_object(value) or value.get("type") != "function":
        return False
    function = value.get("function")
    if not _is_json_object(function):
        return False
    parameters = function.get("parameters")
    if not _is_json_object(parameters):
        return False
    schema_type = parameters.get("type")
    properties = parameters.get("properties")
    return (
        isinstance(function.get("name"), str)
        and isinstance(function.get("description"), str)
        and isinstance(schema_type, str)
        and isinstance(properties, dict)
        and all(_is_json_object(property_schema) for property_schema in properties.values())
    )


def _is_catalog_tool_definitions(value: object) -> TypeIs[list[ToolDefinition]]:
    if not isinstance(value, list):
        return False
    tools: list[object] = list(value)
    return all(_is_catalog_tool_definition(tool) for tool in tools)


def _is_storage_entries(value: object) -> TypeIs[list[StorageEntry]]:
    if not isinstance(value, list):
        return False
    entries: list[object] = list(value)
    return all(isinstance(entry, StorageEntry) for entry in entries)


async def get_agent_tools_for_llm(agent_id: uuid.UUID) -> list[ToolDefinition]:
    if not (
        _is_catalog_tool_definitions(_always_core_tools)
        and _is_catalog_tool_definitions(_feishu_tools)
        and _is_catalog_tool_definitions(_channel_tools)
    ):
        raise ValueError("Agent tool catalog contains an invalid function definition")
    dependencies = _tool_runtime_catalog.CatalogDependencies(
        agent_has_feishu=_agent_has_feishu,
        agent_has_any_channel=_agent_has_any_channel,
        get_computer_os_type=_get_computer_os_type,
        patch_computer_tool_descriptions=_patch_computer_tool_descriptions,
        strip_a2a_msg_type=_strip_a2a_msg_type,
        always_core_tools=_always_core_tools,
        feishu_tools=_feishu_tools,
        channel_tools=_channel_tools,
    )
    return await _tool_runtime_catalog.get_agent_tools_for_llm(agent_id, dependencies=dependencies)


async def _sync_tasks_to_file(agent_id: uuid.UUID, ws: Path) -> None:
    tasks_tool = importlib.import_module("app.services.agent_tool_exec.tasks_tool")
    await tasks_tool._sync_tasks_to_file(agent_id, ws)


# ─── Tool Executors ─────────────────────────────────────────────


async def _get_agent_tenant_id(agent_id: uuid.UUID) -> str | None:
    """Get the agent tenant ID for tenant-scoped shared paths."""
    from app.services.agent_tool_exec import workspace_paths

    return await workspace_paths._get_agent_tenant_id(agent_id)


def _agent_workspace_root(agent_id: uuid.UUID) -> Path:
    """Return the per-agent local path without creating or hydrating it."""
    from app.services.agent_tool_exec import workspace_paths

    return workspace_paths._agent_workspace_root(agent_id, workspace_root=WORKSPACE_ROOT)


def _non_empty_paths(*paths: str | None) -> list[str] | None:
    selected = [path for path in paths if path]
    return selected or None


async def _execute_tool_direct(
    tool_name: str,
    arguments: ToolParameters,
    agent_id: uuid.UUID,
) -> str:
    """Execute a tool directly, bypassing autonomy checks."""
    from app.services.agent_tool_exec.dispatcher import _execute_tool_direct as _impl

    return await _impl(tool_name, arguments, agent_id)


async def execute_tool(
    tool_name: str,
    arguments: ToolParameters,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str = "",
    on_output: ToolOutputCallback | None = None,
) -> str:
    """Execute a tool call and return the result as a string."""
    from app.services.agent_tool_exec.dispatcher import execute_tool as _impl

    return await _impl(
        tool_name,
        arguments,
        agent_id,
        user_id,
        session_id=session_id,
        on_output=on_output,
    )


async def _search_clawhub(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.clawhub import _search_clawhub as extracted

    return await extracted(agent_id, arguments)


async def _install_skill(agent_id: uuid.UUID, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.clawhub import _install_skill as extracted

    return await extracted(agent_id, ws, arguments)


async def _web_search(arguments: ToolParameters, agent_id: uuid.UUID | None = None) -> str:
    web_search = importlib.import_module("app.services.agent_tool_exec.web_search")
    return await web_search._web_search(arguments, agent_id)


async def _search_duckduckgo(query: str, max_results: int) -> str:
    search_providers = importlib.import_module("app.services.agent_tool_exec.search_providers")
    return await search_providers._search_duckduckgo(query, max_results)


async def _get_jina_api_key() -> str:
    search_providers = importlib.import_module("app.services.agent_tool_exec.search_providers")
    return await search_providers._get_jina_api_key()


async def _jina_search(arguments: ToolParameters) -> str:
    web_search = importlib.import_module("app.services.agent_tool_exec.web_search")
    return await web_search._jina_search(arguments)


async def _jina_read(arguments: ToolParameters) -> str:
    web_read = importlib.import_module("app.services.agent_tool_exec.web_read")
    return await web_read._jina_read(arguments)


async def _validate_public_http_url(url: str) -> tuple[str | None, str | None]:
    web_read = importlib.import_module("app.services.agent_tool_exec.web_read")
    return await web_read._validate_public_http_url(url)


def _fallback_extract_visible_text(html: str) -> str:
    web_read = importlib.import_module("app.services.agent_tool_exec.web_read")
    return web_read._fallback_extract_visible_text(html)


def _extract_page_links(html: str, base_url: str, limit: int = 30) -> list[str]:
    web_read = importlib.import_module("app.services.agent_tool_exec.web_read")
    return web_read._extract_page_links(html, base_url, limit=limit)


async def _read_webpage(arguments: ToolParameters) -> str:
    web_read = importlib.import_module("app.services.agent_tool_exec.web_read")
    return await web_read._read_webpage(arguments)


async def _search_tavily(query: str, api_key: str, max_results: int) -> str:
    search_providers = importlib.import_module("app.services.agent_tool_exec.search_providers")
    return await search_providers._search_tavily(query, api_key, max_results)


async def _search_google(query: str, api_key: str, max_results: int, language: str) -> str:
    search_providers = importlib.import_module("app.services.agent_tool_exec.search_providers")
    return await search_providers._search_google(query, api_key, max_results, language)


async def _search_bing(query: str, api_key: str, max_results: int, language: str) -> str:
    search_providers = importlib.import_module("app.services.agent_tool_exec.search_providers")
    return await search_providers._search_bing(query, api_key, max_results, language)


async def _search_exa(query: str, api_key: str, max_results: int) -> str:
    search_providers = importlib.import_module("app.services.agent_tool_exec.search_providers")
    return await search_providers._search_exa(query, api_key, max_results)


async def _exa_search(arguments: ToolParameters, agent_id: uuid.UUID | None = None) -> str:
    web_search = importlib.import_module("app.services.agent_tool_exec.web_search")
    return await web_search._exa_search(arguments, agent_id)


async def _duckduckgo_search_tool(arguments: ToolParameters) -> str:
    web_search = importlib.import_module("app.services.agent_tool_exec.web_search")
    return await web_search._duckduckgo_search_tool(arguments)


async def _tavily_search_tool(arguments: ToolParameters, agent_id: uuid.UUID | None = None) -> str:
    web_search = importlib.import_module("app.services.agent_tool_exec.web_search")
    return await web_search._tavily_search_tool(arguments, agent_id)


async def _google_search_tool(arguments: ToolParameters, agent_id: uuid.UUID | None = None) -> str:
    web_search = importlib.import_module("app.services.agent_tool_exec.web_search")
    return await web_search._google_search_tool(arguments, agent_id)


async def _bing_search_tool(arguments: ToolParameters, agent_id: uuid.UUID | None = None) -> str:
    web_search = importlib.import_module("app.services.agent_tool_exec.web_search")
    return await web_search._bing_search_tool(arguments, agent_id)


async def _execute_mcp_tool(tool_name: str, arguments: ToolParameters, agent_id: uuid.UUID | None = None) -> str:
    from app.services.agent_tool_exec.mcp_tools import _execute_mcp_tool as execute_mcp_tool

    return await execute_mcp_tool(tool_name, arguments, agent_id=agent_id)


async def _execute_via_smithery_connect(
    mcp_url: str, tool_name: str, arguments: ToolParameters, config: JsonObject, agent_id: uuid.UUID | None = None
) -> str:
    from app.services.agent_tool_exec.mcp_smithery import _execute_via_smithery_connect as execute_via_smithery_connect

    return await execute_via_smithery_connect(mcp_url, tool_name, arguments, config, agent_id=agent_id)


async def _smithery_auto_recover(
    api_key: str, mcp_url: str, namespace: str, connection_id: str, agent_id: uuid.UUID | None = None
) -> str | None:
    from app.services.agent_tool_exec.mcp_smithery import _smithery_auto_recover as smithery_auto_recover

    return await smithery_auto_recover(api_key, mcp_url, namespace, connection_id, agent_id=agent_id)


def _normalize_tool_rel_path(rel_path: str) -> str:
    from app.services.agent_tool_exec import workspace_paths

    return workspace_paths._normalize_tool_rel_path(rel_path)


def _collapse_filename_for_match(name: str) -> str:
    from app.services.agent_tool_exec import workspace_paths

    return workspace_paths._collapse_filename_for_match(name)


def _allowed_root_for_tool_path(ws: Path, rel_path: str, tenant_id: str | None = None) -> tuple[Path, str]:
    from app.services.agent_tool_exec import workspace_paths

    return workspace_paths._allowed_root_for_tool_path(
        ws,
        rel_path,
        tenant_id=tenant_id,
        workspace_root=WORKSPACE_ROOT,
        normalize_tool_rel_path=_normalize_tool_rel_path,
    )


def _resolve_tool_source_path(ws: Path, rel_path: str, tenant_id: str | None = None) -> Path:
    from app.services.agent_tool_exec import workspace_paths

    return workspace_paths._resolve_tool_source_path(
        ws,
        rel_path,
        tenant_id=tenant_id,
        allowed_root_for_tool_path=_allowed_root_for_tool_path,
        collapse_filename_for_match=_collapse_filename_for_match,
    )


def _resolve_tool_target_path(ws: Path, rel_path: str, tenant_id: str | None = None) -> Path:
    from app.services.agent_tool_exec import workspace_paths

    return workspace_paths._resolve_tool_target_path(
        ws,
        rel_path,
        tenant_id=tenant_id,
        allowed_root_for_tool_path=_allowed_root_for_tool_path,
    )


def _tool_storage_key(agent_id: uuid.UUID, rel_path: str, tenant_id: str | None = None) -> tuple[str, str, bool]:
    from app.services.agent_tool_exec import workspace_paths

    return workspace_paths._tool_storage_key(
        agent_id,
        rel_path,
        tenant_id=tenant_id,
        normalize_workspace_path_fn=normalize_workspace_path,
        normalize_tool_rel_path=_normalize_tool_rel_path,
        is_enterprise_info_path=_is_enterprise_info_path,
        normalize_storage_key_fn=normalize_storage_key,
    )


def _display_size(size_bytes: int) -> str:
    from app.services.agent_tool_exec import workspace_read

    return workspace_read._display_size(size_bytes)


async def _storage_list_dir(agent_id: uuid.UUID, rel_path: str, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_exec import workspace_read

    return await workspace_read._storage_list_dir(
        agent_id,
        rel_path,
        tenant_id=tenant_id,
        get_storage_backend=get_storage_backend,
        tool_storage_key=_tool_storage_key,
        display_size=_display_size,
    )


async def _storage_read_file(
    agent_id: uuid.UUID,
    rel_path: str,
    tenant_id: str | None = None,
    offset: int = 0,
    limit: int = 2000,
) -> str:
    from app.services.agent_tool_exec import workspace_read

    return await workspace_read._storage_read_file(
        agent_id,
        rel_path,
        tenant_id=tenant_id,
        offset=offset,
        limit=limit,
        get_storage_backend=get_storage_backend,
        tool_storage_key=_tool_storage_key,
    )


async def _storage_walk_files(storage: StorageBackend, root_key: str) -> list[StorageEntry]:
    from app.services.agent_tool_exec import workspace_read

    entries = await workspace_read._storage_walk_files(storage, root_key)
    if not _is_storage_entries(entries):
        raise TypeError("Workspace storage walk returned a non-storage entry")
    return entries


def _relative_storage_display(entry_key: str, base_key: str, display_base: str) -> str:
    from app.services.agent_tool_exec import workspace_read

    return workspace_read._relative_storage_display(entry_key, base_key, display_base)


async def _storage_search_files(
    agent_id: uuid.UUID,
    pattern: str,
    path: str = ".",
    file_pattern: str = "*",
    ignore_case: bool = False,
    tenant_id: str | None = None,
) -> str:
    from app.services.agent_tool_exec import workspace_read

    return await workspace_read._storage_search_files(
        agent_id,
        pattern,
        path=path,
        file_pattern=file_pattern,
        ignore_case=ignore_case,
        tenant_id=tenant_id,
        get_storage_backend=get_storage_backend,
        tool_storage_key=_tool_storage_key,
        storage_walk_files=_storage_walk_files,
        relative_storage_display=_relative_storage_display,
    )


async def _storage_find_files(
    agent_id: uuid.UUID,
    pattern: str,
    path: str = ".",
    tenant_id: str | None = None,
) -> str:
    from app.services.agent_tool_exec import workspace_read

    return await workspace_read._storage_find_files(
        agent_id,
        pattern,
        path=path,
        tenant_id=tenant_id,
        get_storage_backend=get_storage_backend,
        tool_storage_key=_tool_storage_key,
        storage_walk_files=_storage_walk_files,
        relative_storage_display=_relative_storage_display,
        display_size=_display_size,
    )


def _list_files(ws: Path, rel_path: str, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_exec import workspace_local_read

    return workspace_local_read._list_files(ws, rel_path, tenant_id=tenant_id, workspace_root=WORKSPACE_ROOT)


def _read_file(ws: Path, rel_path: str, tenant_id: str | None = None, offset: int = 0, limit: int = 2000) -> str:
    from app.services.agent_tool_exec import workspace_local_read

    return workspace_local_read._read_file(
        ws,
        rel_path,
        tenant_id=tenant_id,
        offset=offset,
        limit=limit,
        resolve_tool_source_path=_resolve_tool_source_path,
    )


_READ_DOCUMENT_MAX_FILE_BYTES = 50 * 1024 * 1024
_READ_DOCUMENT_TIMEOUT_SECONDS = 25
_READ_DOCUMENT_FALLBACK_TIMEOUT_SECONDS = 10
_READ_DOCUMENT_MAX_CELL_CHARS = 500
_READ_DOCUMENT_MAX_COLUMNS = 80
_READ_DOCUMENT_MAX_XLSX_CELLS = 20000


def _safe_document_cell_text(value: object) -> str:
    from app.services.agent_tool_exec.document_reading import _safe_document_cell_text as safe_document_cell_text

    return safe_document_cell_text(value)


def _read_document_sync(ws: Path, rel_path: str, max_chars: int = 8000, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_exec.document_reading import _read_document_sync as read_document_sync

    return read_document_sync(ws, rel_path, max_chars=max_chars, tenant_id=tenant_id)


def _read_document_worker(
    out_queue: mp.Queue[tuple[str, str]], ws_str: str, rel_path: str, max_chars: int, tenant_id: str | None
) -> None:
    from app.services.agent_tool_exec.documents import _read_document_worker as read_document_worker

    read_document_worker(out_queue, ws_str, rel_path, max_chars, tenant_id)


def _read_pdf_fast_sync(ws: Path, rel_path: str, max_chars: int = 8000, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_exec.documents import _read_pdf_fast_sync as read_pdf_fast_sync

    return read_pdf_fast_sync(ws, rel_path, max_chars=max_chars, tenant_id=tenant_id)


def _read_pdf_fast_worker(
    out_queue: mp.Queue[tuple[str, str]], ws_str: str, rel_path: str, max_chars: int, tenant_id: str | None
) -> None:
    from app.services.agent_tool_exec.documents import _read_pdf_fast_worker as read_pdf_fast_worker

    read_pdf_fast_worker(out_queue, ws_str, rel_path, max_chars, tenant_id)


def _read_pdf_fast_with_timeout(ws: Path, rel_path: str, max_chars: int = 8000, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_exec.documents import _read_pdf_fast_with_timeout as read_pdf_fast_with_timeout

    return read_pdf_fast_with_timeout(ws, rel_path, max_chars=max_chars, tenant_id=tenant_id)


def _read_document_with_timeout(ws: Path, rel_path: str, max_chars: int = 8000, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_exec.documents import _read_document_with_timeout as read_document_with_timeout

    return read_document_with_timeout(ws, rel_path, max_chars=max_chars, tenant_id=tenant_id)


async def _read_document(ws: Path, rel_path: str, max_chars: int = 8000, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_exec.documents import _read_document as read_document

    return await read_document(ws, rel_path, max_chars=max_chars, tenant_id=tenant_id)


async def _read_document_from_storage(
    agent_id: uuid.UUID,
    rel_path: str,
    max_chars: int = 8000,
    tenant_id: str | None = None,
) -> str:
    from app.services.agent_tool_exec.documents import _read_document_from_storage as read_document_from_storage

    return await read_document_from_storage(agent_id, rel_path, max_chars=max_chars, tenant_id=tenant_id)


async def _convert_csv_to_xlsx(agent_id: uuid.UUID, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.document_convert import _convert_csv_to_xlsx as convert_csv_to_xlsx

    return await convert_csv_to_xlsx(agent_id, ws, arguments)


async def _convert_html_to_pdf(agent_id: uuid.UUID, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.document_convert import _convert_html_to_pdf as convert_html_to_pdf

    return await convert_html_to_pdf(agent_id, ws, arguments)


async def _convert_html_to_pptx(agent_id: uuid.UUID, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.document_convert import _convert_html_to_pptx as convert_html_to_pptx

    return await convert_html_to_pptx(agent_id, ws, arguments)


async def _convert_markdown_to_docx(agent_id: uuid.UUID, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.document_convert import _convert_markdown_to_docx as convert_markdown_to_docx

    return await convert_markdown_to_docx(agent_id, ws, arguments)


async def _convert_markdown_to_pdf(agent_id: uuid.UUID, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.document_convert import _convert_markdown_to_pdf as convert_markdown_to_pdf

    return await convert_markdown_to_pdf(agent_id, ws, arguments)


def _write_file(ws: Path, rel_path: str, content: str, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_exec import workspace_mutate_sync

    return workspace_mutate_sync._write_file(
        ws,
        rel_path,
        content,
        tenant_id=tenant_id,
        workspace_root=WORKSPACE_ROOT,
        is_enterprise_info_path=_is_enterprise_info_path,
    )


def _delete_file(ws: Path, rel_path: str) -> str:
    from app.services.agent_tool_exec import workspace_mutate_sync

    return workspace_mutate_sync._delete_file(
        ws,
        rel_path,
        is_enterprise_info_path=_is_enterprise_info_path,
    )


def _edit_file(
    ws: Path, rel_path: str, old_string: str, new_string: str, replace_all: bool = False, tenant_id: str | None = None
) -> str:
    from app.services.agent_tool_exec import workspace_mutate_sync

    return workspace_mutate_sync._edit_file(
        ws,
        rel_path,
        old_string,
        new_string,
        replace_all=replace_all,
        tenant_id=tenant_id,
        workspace_root=WORKSPACE_ROOT,
        is_enterprise_info_path=_is_enterprise_info_path,
    )


def _search_files(
    ws: Path,
    pattern: str,
    path: str = ".",
    file_pattern: str = "*",
    ignore_case: bool = False,
    tenant_id: str | None = None,
) -> str:
    from app.services.agent_tool_exec import workspace_local_search

    return workspace_local_search._search_files(
        ws,
        pattern,
        path=path,
        file_pattern=file_pattern,
        ignore_case=ignore_case,
        tenant_id=tenant_id,
        workspace_root=WORKSPACE_ROOT,
    )


def _find_files(ws: Path, pattern: str, path: str = ".", tenant_id: str | None = None) -> str:
    from app.services.agent_tool_exec import workspace_local_search

    return workspace_local_search._find_files(
        ws,
        pattern,
        path=path,
        tenant_id=tenant_id,
        workspace_root=WORKSPACE_ROOT,
    )


async def _manage_tasks(
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    ws: Path,
    args: ToolParameters,
) -> str:
    tasks_tool = importlib.import_module("app.services.agent_tool_exec.tasks_tool")
    return await tasks_tool._manage_tasks(agent_id, user_id, ws, args)


async def _send_feishu_message(agent_id: uuid.UUID, args: ToolParameters) -> str:
    from app.services.agent_tool_exec.feishu_message import _send_feishu_message as _impl

    return await _impl(agent_id, args)


async def _send_platform_message(agent_id: uuid.UUID, args: ToolArgumentMapping) -> str:
    from app.services.agent_tool_exec.platform_messaging import _send_platform_message as extracted

    return await extracted(agent_id, args)


async def _send_channel_message(agent_id: uuid.UUID, args: ToolArgumentMapping) -> str:
    from app.services.agent_tool_exec.channel_messaging import _send_channel_message as _impl

    channel_args: ToolArguments = {
        key: str(value) if isinstance(value, uuid.UUID) else value for key, value in args.items()
    }
    return await _impl(agent_id, channel_args)


# Plaza Tools - Agent Square social feed
# ═══════════════════════════════════════════════════════


async def _plaza_get_new_posts(agent_id: uuid.UUID, arguments: ToolArgumentMapping) -> str:
    """Get recent posts from the Agent Plaza, scoped to agent's tenant."""
    plaza = importlib.import_module("app.services.agent_tool_exec.plaza")
    return await plaza._plaza_get_new_posts(agent_id, arguments)


async def _plaza_create_post(agent_id: uuid.UUID, arguments: ToolArgumentMapping) -> str:
    """Create a new post in the Agent Plaza.

    System agents (is_system=True) are intentionally excluded from Plaza to
    keep the social feed clean - the OKR Agent communicates through Chat and
    reports, not through Plaza posts.
    """
    plaza = importlib.import_module("app.services.agent_tool_exec.plaza")
    return await plaza._plaza_create_post(agent_id, arguments)


async def _plaza_add_comment(agent_id: uuid.UUID, arguments: ToolArgumentMapping) -> str:
    """Add a comment to a plaza post."""
    plaza = importlib.import_module("app.services.agent_tool_exec.plaza")
    return await plaza._plaza_add_comment(agent_id, arguments)


# ─── Code Execution ─────────────────────────────────────────────


def _check_code_safety(language: str, code: str, allow_network: bool = False) -> str | None:
    from app.services.agent_tool_exec.code_exec import _check_code_safety as check_code_safety

    return check_code_safety(language, code, allow_network)


async def _execute_code(
    agent_id: uuid.UUID | None,
    ws: Path,
    arguments: ToolParameters,
    *,
    tool_name: str = "execute_code",
    on_output: ToolOutputCallback | None = None,
) -> str:
    from app.services.agent_tool_exec.code_exec import _execute_code as execute_code

    return await execute_code(agent_id, ws, arguments, tool_name=tool_name, on_output=on_output)


async def _execute_code_legacy(
    ws: Path,
    arguments: ToolParameters,
    allow_network: bool = False,
    max_timeout: int = 60,
    on_output: ToolOutputCallback | None = None,
) -> str:
    from app.services.agent_tool_exec.code_exec import _execute_code_legacy as execute_code_legacy

    return await execute_code_legacy(ws, arguments, allow_network, max_timeout, on_output)


# ─── Resource Discovery Executors ───────────────────────────────


async def _discover_resources(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.mcp_tools import _discover_resources as discover_resources

    return await discover_resources(agent_id, arguments)


async def _import_mcp_server(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.mcp_tools import _import_mcp_server as import_mcp_server

    return await import_mcp_server(agent_id, arguments)


# ─── Trigger Management Handlers (Aware Engine) ────────────────────

MAX_TRIGGERS_PER_AGENT = 20
# ─── Image Upload (ImageKit CDN) ────────────────────────────────


async def _upload_image(agent_id: uuid.UUID, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.images import _upload_image as extracted

    return await extracted(agent_id, ws, arguments)


# ─── Image Generation (Multi-Provider) ────────────────────────────────────────


async def _generate_image(agent_id: uuid.UUID, ws: Path, arguments: ToolParameters, provider: str) -> str:
    from app.services.agent_tool_exec.images import _generate_image as extracted

    return await extracted(agent_id, ws, arguments, provider)


async def _generate_image_siliconflow(api_key: str, model: str, base_url: str, prompt: str, size: str) -> bytes:
    from app.services.agent_tool_exec.images_providers import _generate_image_siliconflow as extracted

    return await extracted(api_key, model, base_url, prompt, size)


async def _generate_image_openai(api_key: str, model: str, base_url: str, prompt: str, size: str) -> bytes:
    from app.services.agent_tool_exec.images_providers import _generate_image_openai as extracted

    return await extracted(api_key, model, base_url, prompt, size)


def _json_path_get(data: JsonValue, path: str) -> JsonValue:
    from app.services.agent_tool_exec.images_custom import _json_path_get as extracted

    return extracted(data, path)


def _render_json_template(template_json: str, variables: dict[str, str]) -> JsonObject:
    from app.services.agent_tool_exec.images_custom import _render_json_template as extracted

    return extracted(template_json, variables)


def _json_structure_preview(data: JsonValue, depth: int = 0) -> JsonValue:
    from app.services.agent_tool_exec.images_custom import _json_structure_preview as extracted

    return extracted(data, depth)


def _find_first_image_reference(data: JsonValue) -> JsonValue:
    from app.services.agent_tool_exec.images_custom import _find_first_image_reference as extracted

    return extracted(data)


async def _custom_image_reference_to_bytes(image_ref: JsonValue, client: object) -> bytes:
    from app.services.agent_tool_exec.images_custom import _custom_image_reference_to_bytes as extracted

    return await extracted(image_ref, client)


async def _generate_image_custom_api(
    api_key: str,
    model: str,
    base_url: str,
    endpoint_path: str,
    request_body_template_json: str,
    response_image_path: str,
    extra_headers_json: str,
    timeout_seconds: int | str,
    prompt: str,
    size: str,
) -> bytes:
    from app.services.agent_tool_exec.images_custom import _generate_image_custom_api as extracted

    return await extracted(
        api_key,
        model,
        base_url,
        endpoint_path,
        request_body_template_json,
        response_image_path,
        extra_headers_json,
        timeout_seconds,
        prompt,
        size,
    )


async def _generate_image_google(api_key: str, model: str, base_url: str, prompt: str, size: str) -> bytes:
    from app.services.agent_tool_exec.images_providers import _generate_image_google as extracted

    return await extracted(api_key, model, base_url, prompt, size)


async def _get_agent_calendar_id(token: str) -> tuple[str | None, str | None]:
    return await _agent_tool_exec_feishu_calendar._get_agent_calendar_id(token)


async def _feishu_resolve_open_id(token: str, email: str) -> str | None:
    return await _agent_tool_exec_feishu_calendar._feishu_resolve_open_id(token, email)


def _iso_to_ts(iso_str: str) -> float:
    return _agent_tool_exec_feishu_calendar._iso_to_ts(iso_str)


async def _get_feishu_credentials(agent_id: uuid.UUID) -> tuple[str, str]:
    return await _agent_tool_exec_feishu_client._get_feishu_credentials(agent_id)


async def _get_feishu_tenant_doc_url(tenant_token: str, doc_token: str, doc_type: str = "docx") -> str:
    return await _agent_tool_exec_feishu_client._get_feishu_tenant_doc_url(tenant_token, doc_token, doc_type)


async def _get_feishu_bitable_url(tenant_token: str, app_token: str, table_id: str = "") -> str:
    return await _agent_tool_exec_feishu_client._get_feishu_bitable_url(tenant_token, app_token, table_id)


def _parse_feishu_url(url: str) -> dict[str, str]:
    return _agent_tool_exec_feishu_client._parse_feishu_url(url)


# ─── Feishu Bitable Tools ──────────────────────────────────────────


async def _resolve_bitable_app_token(agent_id: uuid.UUID, parsed_url: dict[str, str]) -> str | None:
    return await _agent_tool_exec_feishu_bitable._resolve_bitable_app_token(agent_id, parsed_url)


def _check_feishu_err(resp: JsonObject) -> str | None:
    return _agent_tool_exec_feishu_client._check_feishu_err(resp)


async def _bitable_list_tables(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_bitable._bitable_list_tables(agent_id, arguments)


async def _bitable_create_app(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_bitable._bitable_create_app(agent_id, arguments)


async def _bitable_list_fields(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_bitable._bitable_list_fields(agent_id, arguments)


async def _bitable_query_records(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_bitable._bitable_query_records(agent_id, arguments)


async def _bitable_create_record(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_bitable._bitable_create_record(agent_id, arguments)


async def _bitable_update_record(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_bitable._bitable_update_record(agent_id, arguments)


async def _bitable_delete_record(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_bitable._bitable_delete_record(agent_id, arguments)


# ─── Feishu Document Tools ──────────────────────────────────────────


async def _resolve_docx_document_token(agent_id: uuid.UUID, parsed_url: dict[str, str]) -> str | None:
    return await _agent_tool_exec_feishu_docs._resolve_docx_document_token(agent_id, parsed_url)


async def _feishu_read_doc(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_docs._feishu_read_doc(agent_id, arguments)


async def _feishu_create_doc(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_docs._feishu_create_doc(agent_id, arguments)


async def _feishu_append_doc(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_docs._feishu_append_doc(agent_id, arguments)


async def _feishu_wiki_get_node(token_str: str, auth_token: str) -> dict[str, ToolArgumentValue] | None:
    return await _agent_tool_exec_feishu_docs._feishu_wiki_get_node(token_str, auth_token)


async def _feishu_doc_search(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_docs._feishu_doc_search(agent_id, arguments)


async def _feishu_wiki_list(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_docs._feishu_wiki_list(agent_id, arguments)


async def _feishu_doc_read(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_docs._feishu_doc_read(agent_id, arguments)


async def _feishu_doc_create(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_docs._feishu_doc_create(agent_id, arguments)


def _parse_inline_markdown(text: str) -> list[ToolArgumentValue]:
    return _agent_tool_exec_feishu_markdown._parse_inline_markdown(text)


def _markdown_to_feishu_blocks(markdown: str) -> list[JsonObject]:
    return _agent_tool_exec_feishu_markdown._markdown_to_feishu_blocks(markdown)


async def _feishu_doc_append(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_docs._feishu_doc_append(agent_id, arguments)


# ─── Feishu Drive/Calendar/Approval/Contacts Compatibility ─────────────────────


async def _feishu_drive_share(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_drive._feishu_drive_share(agent_id, arguments)


async def _feishu_drive_delete(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_drive._feishu_drive_delete(agent_id, arguments)


async def _feishu_calendar_list(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_calendar._feishu_calendar_list(agent_id, arguments)


async def _feishu_calendar_create(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_calendar._feishu_calendar_create(agent_id, arguments)


async def _feishu_calendar_update(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_calendar._feishu_calendar_update(agent_id, arguments)


async def _feishu_calendar_delete(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_calendar._feishu_calendar_delete(agent_id, arguments)


async def _feishu_approval_create(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_approvals._feishu_approval_create(agent_id, arguments)


async def _feishu_approval_query(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_approvals._feishu_approval_query(agent_id, arguments)


async def _feishu_approval_get(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_approvals._feishu_approval_get(agent_id, arguments)


async def _feishu_user_search(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_feishu_contacts._feishu_user_search(agent_id, arguments)


async def _feishu_contacts_refresh(agent_id: uuid.UUID) -> None:
    await _agent_tool_exec_feishu_contacts._feishu_contacts_refresh(agent_id)


# ─── Email Tool Helpers ─────────────────────────────────────


async def _get_email_config(agent_id: uuid.UUID) -> JsonObject:
    from app.services.agent_tool_exec.email_tools import _get_email_config as extracted

    return await extracted(agent_id)


async def _handle_email_tool(tool_name: str, agent_id: uuid.UUID, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.email_tools import _handle_email_tool as extracted

    return await extracted(tool_name, agent_id, ws, arguments)


# ── Pages: public HTML hosting ──────────────────────────


async def _publish_page(agent_id: uuid.UUID, user_id: uuid.UUID, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.publish import _publish_page as _impl

    return await _impl(agent_id, user_id, ws, arguments)


async def _list_published_pages(agent_id: uuid.UUID) -> str:
    from app.services.agent_tool_exec.publish import _list_published_pages as _impl

    return await _impl(agent_id)


# ─── AgentBay Tool Handlers ─────────────────────────────────────


def _agentbay_normalize_image_bytes(data: object) -> bytes | None:
    from app.services.agent_tool_exec.agentbay_media import _agentbay_normalize_image_bytes as extracted

    return extracted(data)


def _agentbay_save_image_to_workspace(
    *, agent_id: uuid.UUID, ws: Path, raw_bytes: bytes, prefix: str, label: str
) -> str:
    from app.services.agent_tool_exec.agentbay_media import _agentbay_save_image_to_workspace as extracted

    return extracted(agent_id=agent_id, ws=ws, raw_bytes=raw_bytes, prefix=prefix, label=label)


async def _agentbay_browser_navigate(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_browser import _agentbay_browser_navigate as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_browser_screenshot(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_browser import _agentbay_browser_screenshot as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_browser_save_screenshot(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_browser import _agentbay_browser_save_screenshot as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_browser_click(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_browser import _agentbay_browser_click as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_browser_type(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_browser import _agentbay_browser_type as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_browser_extract(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_browser import _agentbay_browser_extract as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_browser_observe(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_browser import _agentbay_browser_observe as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_browser_login(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_browser import _agentbay_browser_login as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_code_execute(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_code import _agentbay_code_execute as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_code_write_file(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_code import _agentbay_code_write_file as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_code_read_file(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_code import _agentbay_code_read_file as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_code_edit_file(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_code import _agentbay_code_edit_file as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_command_exec(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_code import _agentbay_command_exec as extracted

    return await extracted(agent_id, ws, arguments)


def _agentbay_extract_screen_dimensions(screen_data: object) -> tuple[int | None, int | None, str]:
    from app.services.agent_tool_exec.agentbay_screen import _agentbay_extract_screen_dimensions as extracted

    return extracted(screen_data)


async def _agentbay_get_screen_metadata(client: AgentBayClient) -> tuple[int | None, int | None, str]:
    from app.services.agent_tool_exec.agentbay_screen import _agentbay_get_screen_metadata as extracted

    return await extracted(client)


def _agentbay_image_dimensions(raw_bytes: bytes) -> tuple[int | None, int | None]:
    from app.services.agent_tool_exec.agentbay_screen import _agentbay_image_dimensions as extracted

    return extracted(raw_bytes)


def _agentbay_crop_image_bytes(
    raw_bytes: bytes, *, x: int, y: int, width: int, height: int
) -> tuple[bytes, tuple[int, int, int, int], int] | None:
    from app.services.agent_tool_exec.agentbay_screen import _agentbay_crop_image_bytes as extracted

    return extracted(raw_bytes, x=x, y=y, width=width, height=height)


def _agentbay_expand_precision_crop(
    x: int, y: int, width: int, height: int, *, min_width: int = 360, min_height: int = 240
) -> tuple[int, int, int, int]:
    from app.services.agent_tool_exec.agentbay_screen import _agentbay_expand_precision_crop as extracted

    return extracted(x, y, width, height, min_width=min_width, min_height=min_height)


def _agentbay_desktop_coordinate_note(
    screen_note: str,
    image_width: int | None = None,
    image_height: int | None = None,
    crop: tuple[int, int, int, int] | None = None,
) -> str:
    from app.services.agent_tool_exec.agentbay_screen import _agentbay_desktop_coordinate_note as extracted

    return extracted(screen_note, image_width, image_height, crop)


async def _agentbay_computer_screenshot(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_screen import _agentbay_computer_screenshot as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_computer_save_screenshot(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_screen import _agentbay_computer_save_screenshot as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_computer_precision_screenshot(
    agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters
) -> str:
    from app.services.agent_tool_exec.agentbay_screen import _agentbay_computer_precision_screenshot as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_computer_click(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_computer import _agentbay_computer_click as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_computer_input_text(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_computer import _agentbay_computer_input_text as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_computer_press_keys(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_computer import _agentbay_computer_press_keys as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_computer_scroll(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_computer import _agentbay_computer_scroll as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_computer_move_mouse(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_computer import _agentbay_computer_move_mouse as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_computer_drag_mouse(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_computer import _agentbay_computer_drag_mouse as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_computer_get_screen_size(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_computer import _agentbay_computer_get_screen_size as extracted

    return await extracted(agent_id, ws, arguments)


def _agentbay_normalize_text(value: object) -> str:
    from app.services.agent_tool_exec.agentbay_apps import _agentbay_normalize_text as extracted

    return extracted(value)


def _agentbay_app_field(app: JsonObject, *keys: str) -> str:
    from app.services.agent_tool_exec.agentbay_apps import _agentbay_app_field as extracted

    return extracted(app, *keys)


def _agentbay_format_apps(apps: list[ToolArgumentValue], limit: int = 40) -> str:
    from app.services.agent_tool_exec.agentbay_apps import _agentbay_format_apps as extracted

    return extracted(apps, limit)


def _agentbay_find_installed_app_match(query: str, apps: list[ToolArgumentValue]) -> tuple[JsonObject | None, float]:
    from app.services.agent_tool_exec.agentbay_apps import _agentbay_find_installed_app_match as extracted

    return extracted(query, apps)


def _agentbay_uncertain_start_error(error_message: str) -> bool:
    from app.services.agent_tool_exec.agentbay_apps import _agentbay_uncertain_start_error as extracted

    return extracted(error_message)


async def _agentbay_visible_apps_note(client: AgentBayClient) -> str:
    from app.services.agent_tool_exec.agentbay_apps import _agentbay_visible_apps_note as extracted

    return await extracted(client)


async def _agentbay_computer_start_app(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_apps import _agentbay_computer_start_app as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_computer_get_installed_apps(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_apps import _agentbay_computer_get_installed_apps as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_computer_list_visible_apps(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_apps import _agentbay_computer_list_visible_apps as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_computer_get_cursor_position(
    agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters
) -> str:
    from app.services.agent_tool_exec.agentbay_windows import _agentbay_computer_get_cursor_position as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_computer_get_active_window(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_windows import _agentbay_computer_get_active_window as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_computer_list_windows(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_windows import _agentbay_computer_list_windows as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_computer_activate_window(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_windows import _agentbay_computer_activate_window as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_computer_close_window(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_windows import _agentbay_computer_close_window as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_computer_dismiss_dialog(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_windows import _agentbay_computer_dismiss_dialog as extracted

    return await extracted(agent_id, ws, arguments)


async def _agentbay_file_transfer(agent_id: uuid.UUID | None, ws: Path, arguments: ToolParameters) -> str:
    from app.services.agent_tool_exec.agentbay_files import _agentbay_file_transfer as extracted

    return await extracted(agent_id, ws, arguments)


# ─── OKR Tools ───────────────────────────────────────────────────────────────


async def _get_agent_owner_info(agent_id: uuid.UUID) -> tuple[str, str]:
    return await _agent_tool_exec_okr_access._get_agent_owner_info(agent_id)


def _compute_okr_period_bounds(frequency: str, length_days: int | None):
    return _agent_tool_exec_okr_access._compute_okr_period_bounds(frequency, length_days)


async def _get_okr(agent_id: uuid.UUID | None, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_okr_read._get_okr(agent_id, arguments)


async def _get_my_okr(agent_id: uuid.UUID | None, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_okr_read._get_my_okr(agent_id, arguments)


async def _load_okr_request_context(
    db: object | None,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
) -> _agent_tool_exec_okr_access._OKRRequestContext:
    return await _agent_tool_exec_okr_access._load_okr_request_context(db, agent_id, user_id)


def _okr_permission_denied(message: str) -> str:
    return _agent_tool_exec_okr_access._okr_permission_denied(message)


def _can_access_existing_okr_target(
    ctx: _agent_tool_exec_okr_access._OKRRequestContext, owner_type: str, owner_id: uuid.UUID | None
) -> str | None:
    return _agent_tool_exec_okr_access._can_access_existing_okr_target(ctx, owner_type, owner_id)


def _can_create_okr_target(
    ctx: _agent_tool_exec_okr_access._OKRRequestContext, owner_type: str, owner_id: uuid.UUID | None
) -> str | None:
    return _agent_tool_exec_okr_access._can_create_okr_target(ctx, owner_type, owner_id)


async def _update_kr_progress(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_okr_write._update_kr_progress(agent_id, user_id, arguments)


async def _update_kr_content(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_okr_write._update_kr_content(agent_id, user_id, arguments)


async def _collect_okr_progress(agent_id: uuid.UUID | None) -> str:
    return await _agent_tool_exec_okr_reports._collect_okr_progress(agent_id)


async def _generate_okr_report(agent_id: uuid.UUID | None, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_okr_reports._generate_okr_report(agent_id, arguments)


async def _generate_monthly_okr_report(agent_id: uuid.UUID | None) -> str:
    return await _agent_tool_exec_okr_reports._generate_monthly_okr_report(agent_id)


async def _get_okr_settings_tool(agent_id: uuid.UUID | None) -> str:
    return await _agent_tool_exec_okr_read._get_okr_settings_tool(agent_id)


async def _create_objective(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_okr_write._create_objective(agent_id, user_id, arguments)


async def _create_key_result(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_okr_write._create_key_result(agent_id, user_id, arguments)


async def _update_objective(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_okr_write._update_objective(agent_id, user_id, arguments)


async def _update_any_kr_progress(
    agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: ToolParameters
) -> str:
    return await _agent_tool_exec_okr_write._update_any_kr_progress(agent_id, user_id, arguments)


async def _upsert_member_daily_report(agent_id: uuid.UUID | None, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_okr_reports._upsert_member_daily_report(agent_id, arguments)


# ── Vercel & Neon Deploy Helper Functions ──


async def _get_vercel_token(agent_id: uuid.UUID, tool_name: str) -> str | None:
    return await _agent_tool_exec_deploy_ops._get_vercel_token(agent_id, tool_name)


async def _get_vercel_quota_summary(vercel_token: str) -> str:
    return await _agent_tool_exec_deploy_ops._get_vercel_quota_summary(vercel_token)


async def _check_neon_quota_limit(api_key: str) -> tuple[bool, str]:
    return await _agent_tool_exec_deploy_ops._check_neon_quota_limit(api_key)


async def _vercel_deploy(agent_id: uuid.UUID, ws: Path, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_deploy._vercel_deploy(agent_id, ws, arguments)


async def _vercel_list_deployments(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_deploy._vercel_list_deployments(agent_id, arguments)


async def _vercel_get_deploy_logs(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_deploy._vercel_get_deploy_logs(agent_id, arguments)


async def _vercel_set_env(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_deploy_ops._vercel_set_env(agent_id, arguments)


async def _vercel_manage_domain(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_deploy_ops._vercel_manage_domain(agent_id, arguments)


async def _neon_create_database(agent_id: uuid.UUID, arguments: ToolParameters) -> str:
    return await _agent_tool_exec_deploy_ops._neon_create_database(agent_id, arguments)
