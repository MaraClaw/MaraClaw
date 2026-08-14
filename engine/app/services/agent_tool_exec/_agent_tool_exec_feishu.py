from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Final, cast

from app.core.json_types import object_attr

from . import (
    _agent_tool_exec_feishu_approvals as _feishu_approvals,
    _agent_tool_exec_feishu_bitable as _feishu_bitable,
    _agent_tool_exec_feishu_calendar as _feishu_calendar,
    _agent_tool_exec_feishu_contacts as _feishu_contacts,
    _agent_tool_exec_feishu_docs as _feishu_docs,
    _agent_tool_exec_feishu_drive as _feishu_drive,
    feishu_message as _feishu_message,
)
from .registry import ToolArguments, ToolOutputCallback, register

type FeishuHelper = Callable[[uuid.UUID, ToolArguments], Awaitable[str]]

_BITABLE_HANDLERS: Final[tuple[tuple[str, str], ...]] = (
    ("bitable_create_app", "_bitable_create_app"),
    ("bitable_list_tables", "_bitable_list_tables"),
    ("bitable_list_fields", "_bitable_list_fields"),
    ("bitable_query_records", "_bitable_query_records"),
    ("bitable_create_record", "_bitable_create_record"),
    ("bitable_update_record", "_bitable_update_record"),
    ("bitable_delete_record", "_bitable_delete_record"),
)

_DOC_HANDLERS: Final[tuple[tuple[str, str], ...]] = (
    ("feishu_doc_search", "_feishu_doc_search"),
    ("feishu_wiki_list", "_feishu_wiki_list"),
    ("feishu_doc_read", "_feishu_doc_read"),
    ("feishu_doc_create", "_feishu_doc_create"),
    ("feishu_doc_append", "_feishu_doc_append"),
)

_TASK12_HANDLERS: Final[tuple[tuple[str, ModuleType, str], ...]] = (
    ("feishu_drive_share", _feishu_drive, "_feishu_drive_share"),
    ("feishu_drive_delete", _feishu_drive, "_feishu_drive_delete"),
    ("feishu_user_search", _feishu_contacts, "_feishu_user_search"),
    ("feishu_calendar_list", _feishu_calendar, "_feishu_calendar_list"),
    ("feishu_calendar_create", _feishu_calendar, "_feishu_calendar_create"),
    ("feishu_calendar_update", _feishu_calendar, "_feishu_calendar_update"),
    ("feishu_calendar_delete", _feishu_calendar, "_feishu_calendar_delete"),
    ("feishu_approval_create", _feishu_approvals, "_feishu_approval_create"),
    ("feishu_approval_query", _feishu_approvals, "_feishu_approval_query"),
    ("feishu_approval_get", _feishu_approvals, "_feishu_approval_get"),
)


def _feishu_helper(module: object, helper_name: str) -> FeishuHelper:
    loaded = object_attr(module, helper_name)
    if not callable(loaded):
        raise TypeError(f"Feishu helper {helper_name} is not callable")
    return cast(FeishuHelper, loaded)


@register("send_feishu_message")
async def send_feishu_message(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _feishu_message._send_feishu_message(agent_id, arguments)


def _register_bitable_handler(tool_name: str, helper_name: str) -> None:
    @register(tool_name)
    async def handler(
        *,
        arguments: ToolArguments,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str,
        on_output: ToolOutputCallback | None,
    ) -> str:
        del user_id, session_id, on_output
        helper = _feishu_helper(_feishu_bitable, helper_name)
        return await helper(agent_id, arguments)


def _register_doc_handler(tool_name: str, helper_name: str) -> None:
    @register(tool_name)
    async def handler(
        *,
        arguments: ToolArguments,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str,
        on_output: ToolOutputCallback | None,
    ) -> str:
        del user_id, session_id, on_output
        helper = _feishu_helper(_feishu_docs, helper_name)
        return await helper(agent_id, arguments)


def _register_module_handler(tool_name: str, module: ModuleType, helper_name: str) -> None:
    @register(tool_name)
    async def handler(
        *,
        arguments: ToolArguments,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str,
        on_output: ToolOutputCallback | None,
    ) -> str:
        del user_id, session_id, on_output
        helper = _feishu_helper(module, helper_name)
        return await helper(agent_id, arguments)


for _tool_name, _helper_name in _BITABLE_HANDLERS:
    _register_bitable_handler(_tool_name, _helper_name)

for _tool_name, _helper_name in _DOC_HANDLERS:
    _register_doc_handler(_tool_name, _helper_name)

for _tool_name, _module, _helper_name in _TASK12_HANDLERS:
    _register_module_handler(_tool_name, _module, _helper_name)
