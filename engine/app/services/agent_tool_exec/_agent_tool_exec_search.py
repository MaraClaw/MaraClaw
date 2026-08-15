from __future__ import annotations

import importlib
import uuid
from typing import Protocol, TypeIs

from . import workspace_read
from .registry import ToolArguments, ToolOutputCallback, current_execution_context, register


class _WebReadModule(Protocol):
    async def _read_webpage(self, arguments: ToolArguments) -> str: ...


def _has_callables(value: object, *names: str) -> bool:
    return all(callable(getattr(value, name, None)) for name in names)


def _is_web_read_module(value: object) -> TypeIs[_WebReadModule]:
    return _has_callables(value, "_read_webpage")


def _load_search_module(name: str) -> object:
    return importlib.import_module(name)


class _XSearchModule(Protocol):
    async def _search_x(self, agent_id: uuid.UUID, arguments: ToolArguments) -> str: ...


def _is_x_search_module(value: object) -> TypeIs[_XSearchModule]:
    return _has_callables(value, "_search_x")


_loaded_web_read = _load_search_module("app.services.agent_tool_exec.web_read")
if not _is_web_read_module(_loaded_web_read):
    raise TypeError("web_read module is missing required handlers")
_web_read_module = _loaded_web_read

_loaded_x_search = _load_search_module("app.services.agent_tool_exec.x_search")
if not _is_x_search_module(_loaded_x_search):
    raise TypeError("x_search module is missing required handlers")
_x_search_module = _loaded_x_search


def _string_argument(arguments: ToolArguments, name: str, default: str) -> str:
    value = arguments.get(name, default)
    return value if isinstance(value, str) else default


@register("search_files")
async def search_files(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    from app.services import agent_tools

    pattern = _string_argument(arguments, "pattern", "")
    if not pattern:
        return "❌ Missing required argument 'pattern' for search_files"
    context = current_execution_context()
    tenant_id = context.tenant_id if context is not None else await agent_tools._get_agent_tenant_id(agent_id)
    return await workspace_read._storage_search_files(
        agent_id,
        pattern,
        path=_string_argument(arguments, "path", "."),
        file_pattern=_string_argument(arguments, "file_pattern", "*"),
        ignore_case=arguments.get("ignore_case") is True,
        tenant_id=tenant_id,
        get_storage_backend=agent_tools.get_storage_backend,
        tool_storage_key=agent_tools._tool_storage_key,
        storage_walk_files=agent_tools._storage_walk_files,
        relative_storage_display=agent_tools._relative_storage_display,
    )


@register("find_files")
async def find_files(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    from app.services import agent_tools

    pattern = _string_argument(arguments, "pattern", "")
    if not pattern:
        return "❌ Missing required argument 'pattern' for find_files"
    context = current_execution_context()
    tenant_id = context.tenant_id if context is not None else await agent_tools._get_agent_tenant_id(agent_id)
    return await workspace_read._storage_find_files(
        agent_id,
        pattern,
        path=_string_argument(arguments, "path", "."),
        tenant_id=tenant_id,
        get_storage_backend=agent_tools.get_storage_backend,
        tool_storage_key=agent_tools._tool_storage_key,
        storage_walk_files=agent_tools._storage_walk_files,
        relative_storage_display=agent_tools._relative_storage_display,
        display_size=agent_tools._display_size,
    )


@register("read_webpage")
async def read_webpage(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del agent_id, user_id, session_id, on_output
    return await _web_read_module._read_webpage(arguments)


@register("search_x")
async def search_x(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _x_search_module._search_x(agent_id, arguments)
