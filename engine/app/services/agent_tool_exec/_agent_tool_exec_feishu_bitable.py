from __future__ import annotations

import importlib
import json
import uuid
from dataclasses import dataclass
from typing import cast

from app.core.json_types import JsonObject, json_loads_object, json_loads_value, object_attr
from app.services import agent_tools
from app.services.feishu_service import FeishuService

from .registry import ToolArguments, ToolArgumentValue, tool_arg_str


def _string_argument(arguments: ToolArguments, name: str, default: str = "") -> str:
    value = arguments.get(name)
    return value if isinstance(value, str) else default


def _integer_argument(arguments: ToolArguments, name: str, default: int) -> int:
    value = arguments.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


@dataclass(frozen=True, slots=True)
class _RecordWrite:
    app_token: str
    table_id: str
    fields: dict[str, ToolArgumentValue]
    create: bool
    record_id: str = ""


def _feishu_service() -> FeishuService:
    module = importlib.import_module("app.services.feishu_service")
    return cast(FeishuService, object_attr(module, "feishu_service"))


def _nested_mapping(value: object) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _object_items(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _fields_mapping(value: object) -> dict[str, ToolArgumentValue]:
    return value if isinstance(value, dict) else {}


async def _resolve_bitable_app_token(agent_id: uuid.UUID, parsed_url: dict[str, str]) -> str | None:
    app_token = parsed_url.get("app_token")
    if app_token:
        return app_token
    wiki_token = parsed_url.get("wiki_token")
    if wiki_token:
        app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
        if app_id and app_secret:
            token = await _feishu_service().get_tenant_access_token(app_id, app_secret)
            node_info = await agent_tools._feishu_wiki_get_node(wiki_token, token)
            obj_token = tool_arg_str(node_info.get("obj_token")) if node_info else None
            if obj_token:
                return obj_token
    return None


def _filters_from(filter_info: ToolArgumentValue) -> dict[str, ToolArgumentValue]:
    if isinstance(filter_info, dict):
        return filter_info
    if isinstance(filter_info, str) and filter_info.strip():
        try:
            loaded = json_loads_value(filter_info)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


async def _resolved_app_and_table(agent_id: uuid.UUID, arguments: ToolArguments) -> tuple[str | None, str | None]:
    parsed = agent_tools._parse_feishu_url(_string_argument(arguments, "url"))
    app_token = await agent_tools._resolve_bitable_app_token(agent_id, parsed)
    table_id = _string_argument(arguments, "table_id") or parsed.get("table_id")
    return app_token, table_id


async def _bitable_list_tables(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    parsed = agent_tools._parse_feishu_url(_string_argument(arguments, "url"))
    app_token = await agent_tools._resolve_bitable_app_token(agent_id, parsed)
    if not app_token:
        return "Failed: Could not extract Bitable app_token from the URL (also could not resolve wiki_token)."

    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    try:
        service = _feishu_service()
        resp = await service.bitable_list_tables(app_id, app_secret, app_token)
        err = agent_tools._check_feishu_err(resp)
        if err:
            return err
        tables = _object_items(_nested_mapping(resp.get("data")).get("items"))
        if not tables:
            return "OK: No tables found in this Bitable."
        lines = [f"- {table.get('name')} (ID: {table.get('table_id')})" for table in tables]
        tenant_token = await service.get_tenant_access_token(app_id, app_secret)
        bitable_url = await agent_tools._get_feishu_bitable_url(tenant_token, app_token)
        return "OK: Tables in this Bitable:\n" + "\n".join(lines) + f"\n\n🔗 多维表格链接: {bitable_url}"
    except Exception as error:
        return f"Failed: {str(error)[:300]}"


async def _bitable_create_app(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    name = _string_argument(arguments, "name").strip()
    if not name:
        return "Failed: Missing required argument 'name' - please provide a name for the new Bitable."
    folder_token = _string_argument(arguments, "folder_token").strip()
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    try:
        service = _feishu_service()
        resp = await service.bitable_create_app(app_id, app_secret, name, folder_token)
        err = agent_tools._check_feishu_err(resp)
        if err:
            return err
        app_info = _nested_mapping(_nested_mapping(resp.get("data")).get("app"))
        app_token = tool_arg_str(app_info.get("app_token")) or ""
        bitable_url = tool_arg_str(app_info.get("url")) or ""
        default_table_id = tool_arg_str(app_info.get("default_table_id")) or ""
        if not app_token:
            return f"Failed: Bitable created but could not extract app_token from response: {resp}"
        if not bitable_url:
            tenant_token = await service.get_tenant_access_token(app_id, app_secret)
            bitable_url = await agent_tools._get_feishu_bitable_url(tenant_token, app_token)
        result = f"OK: Bitable created successfully!\nName: {name}\nApp Token: {app_token}\nURL: {bitable_url}"
        if default_table_id:
            result += f"\nDefault Table ID: {default_table_id}"
        return result
    except Exception as error:
        return f"Failed: {str(error)[:300]}"


async def _bitable_list_fields(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    app_token, table_id = await _resolved_app_and_table(agent_id, arguments)
    if not app_token:
        return "Failed: Could not extract Bitable app_token from the URL."
    if not table_id:
        return "Failed: table_id is required. Provide it as a parameter or include it in the URL."
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    try:
        resp = await _feishu_service().bitable_list_fields(app_id, app_secret, app_token, table_id)
        err = agent_tools._check_feishu_err(resp)
        if err:
            return err
        fields = _object_items(_nested_mapping(resp.get("data")).get("items"))
        if not fields:
            return "OK: No fields found in this table."
        lines = [
            f"- {field.get('field_name')} (type: {field.get('type')}, ID: {field.get('field_id')})" for field in fields
        ]
        return "OK: Fields in this table:\n" + "\n".join(lines)
    except Exception as error:
        return f"Failed: {str(error)[:300]}"


async def _bitable_query_records(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    app_token, table_id = await _resolved_app_and_table(agent_id, arguments)
    if not app_token or not table_id:
        return "Failed: Could not resolve app_token or table_id from the provided parameters/URL."
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    try:
        resp = await _feishu_service().bitable_query_records(
            app_id,
            app_secret,
            app_token,
            table_id,
            _filters_from(arguments.get("filter_info", "")),
        )
        err = agent_tools._check_feishu_err(resp)
        if err:
            return err
        records = _object_items(_nested_mapping(resp.get("data")).get("items"))
        if not records:
            return "OK: No matching records found."
        lines = [
            f"Record {record.get('record_id')}: {json.dumps(record.get('fields', {}), ensure_ascii=False)}"
            for record in records[: _integer_argument(arguments, "max_results", 100)]
        ]
        return "OK: Query results:\n" + "\n".join(lines)
    except Exception as error:
        return f"Failed: {str(error)[:300]}"


async def _bitable_create_record(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    app_token, table_id = await _resolved_app_and_table(agent_id, arguments)
    if not app_token or not table_id:
        return "Failed: Could not resolve app_token or table_id from the provided parameters/URL."
    try:
        parsed_fields = json_loads_object(_string_argument(arguments, "fields", "{}"))
    except json.JSONDecodeError:
        return "Failed: The 'fields' parameter is not valid JSON."
    return await _write_record(
        agent_id,
        _RecordWrite(app_token=app_token, table_id=table_id, fields=_fields_mapping(parsed_fields), create=True),
    )


async def _bitable_update_record(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    app_token, table_id = await _resolved_app_and_table(agent_id, arguments)
    record_id = _string_argument(arguments, "record_id")
    if not app_token or not table_id or not record_id:
        return "Failed: Missing required parameters. Need app_token (from URL), table_id, and record_id."
    try:
        parsed_fields = json_loads_object(_string_argument(arguments, "fields", "{}"))
    except json.JSONDecodeError:
        return "Failed: The 'fields' parameter is not valid JSON."
    return await _write_record(
        agent_id,
        _RecordWrite(
            app_token=app_token,
            table_id=table_id,
            fields=_fields_mapping(parsed_fields),
            create=False,
            record_id=record_id,
        ),
    )


async def _write_record(
    agent_id: uuid.UUID,
    write: _RecordWrite,
) -> str:
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    try:
        service = _feishu_service()
        if write.create:
            resp = await service.bitable_create_record(
                app_id, app_secret, write.app_token, write.table_id, write.fields
            )
            verb = "created"
        else:
            resp = await service.bitable_update_record(
                app_id,
                app_secret,
                write.app_token,
                write.table_id,
                write.record_id,
                write.fields,
            )
            verb = "updated"
        err = agent_tools._check_feishu_err(resp)
        if err:
            return err
        record = _nested_mapping(_nested_mapping(resp.get("data")).get("record"))
        tenant_token = await service.get_tenant_access_token(app_id, app_secret)
        bitable_url = await agent_tools._get_feishu_bitable_url(tenant_token, write.app_token, write.table_id)
        return (
            f"OK: Record {verb}. Record ID: {record.get('record_id')}\n"
            + f"Fields: {json.dumps(record.get('fields', {}), ensure_ascii=False)}\n"
            + f"🔗 多维表格链接: {bitable_url}"
        )
    except Exception as error:
        return f"Failed: {str(error)[:300]}"


async def _bitable_delete_record(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    app_token, table_id = await _resolved_app_and_table(agent_id, arguments)
    record_id = _string_argument(arguments, "record_id")
    if not app_token or not table_id or not record_id:
        return "Failed: Missing required parameters. Need app_token (from URL), table_id, and record_id."
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    try:
        service = _feishu_service()
        resp = await service.bitable_delete_record(app_id, app_secret, app_token, table_id, record_id)
        err = agent_tools._check_feishu_err(resp)
        if err:
            return err
        tenant_token = await service.get_tenant_access_token(app_id, app_secret)
        bitable_url = await agent_tools._get_feishu_bitable_url(tenant_token, app_token, table_id)
        return f"OK: Record {record_id} deleted successfully.\n🔗 多维表格链接: {bitable_url}"
    except Exception as error:
        return f"Failed: {str(error)[:300]}"
