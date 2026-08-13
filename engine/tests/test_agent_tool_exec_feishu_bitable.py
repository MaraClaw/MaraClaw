from __future__ import annotations

import importlib
import json
import uuid
from collections import deque

from app.core.json_types import JsonObject
from app.services import agent_tools
from app.services.agent_tool_exec import registry
from app.services.agent_tool_exec.registry import ToolArgumentValue

BITABLE_URL = "https://example.feishu.cn/base/app_token_1?table=tblA&view=vew1"
BITABLE_LINK = "https://tenant.example/base/app_token_1"
BITABLE_TABLE_LINK = "https://tenant.example/base/app_token_1?table=tblA"
PERMISSION_MESSAGE = (
    "Failed: Permission denied (code: 99991663, msg: no permission to access resource). "
    "The bot app does not have access to this document/Bitable. "
    "CRITICAL INSTRUCTION for the Agent: You MUST explicitly output ALL of the following "
    "step-by-step instructions to the user in your reply. Do NOT omit or summarize any step:\n\n"
    "---\n"
    "**The bot needs to be added to this document/table as an authorized app. "
    "Please follow these steps:**\n\n"
    "1. Open the target document or Bitable in the Feishu client (web or desktop).\n"
    "2. Click the **「...」** menu button in the top-right corner of the page.\n"
    "3. In the dropdown menu, hover over **「更多」** (More) at the bottom.\n"
    "4. In the sub-menu that appears, click **「添加文档应用」** (Add Document App).\n"
    "5. In the search box, type the name of your Feishu bot app (the one bound to this Agent's channel), "
    "then click to add it.\n"
    "6. After adding, retry the same operation.\n\n"
    "If you cannot find 「添加文档应用」, it means the document owner may need to enable this option, "
    "or you can try: click **「分享」** (Share) button -> invite the bot app directly.\n"
    "---"
)

type BitableCallArguments = (
    tuple[()]
    | tuple[str, str, str]
    | tuple[str, str, str, str]
    | tuple[str, str, str, str, dict[str, ToolArgumentValue]]
    | tuple[str, str, str, str, str]
    | tuple[str, str, str, str, str, dict[str, ToolArgumentValue]]
)
type BitableCall = tuple[str, BitableCallArguments]


class _FakeFeishuService:
    def __init__(self, responses: dict[str, list[JsonObject]]) -> None:
        self._responses = {name: deque(values) for name, values in responses.items()}
        self.calls: list[BitableCall] = []
        self.token_calls: list[tuple[str, str]] = []

    def _next(self, name: str) -> JsonObject:
        self.calls.append((name, ()))
        return self._responses[name].popleft()

    async def get_tenant_access_token(self, app_id: str, app_secret: str) -> str:
        self.token_calls.append((app_id, app_secret))
        return "tenant-token"

    async def bitable_create_app(self, app_id: str, app_secret: str, name: str, folder_token: str) -> JsonObject:
        self.calls.append(("bitable_create_app", (app_id, app_secret, name, folder_token)))
        return self._responses["bitable_create_app"].popleft()

    async def bitable_list_tables(self, app_id: str, app_secret: str, app_token: str) -> JsonObject:
        self.calls.append(("bitable_list_tables", (app_id, app_secret, app_token)))
        return self._responses["bitable_list_tables"].popleft()

    async def bitable_list_fields(self, app_id: str, app_secret: str, app_token: str, table_id: str) -> JsonObject:
        self.calls.append(("bitable_list_fields", (app_id, app_secret, app_token, table_id)))
        return self._responses["bitable_list_fields"].popleft()

    async def bitable_query_records(
        self, app_id: str, app_secret: str, app_token: str, table_id: str, filters: dict[str, ToolArgumentValue]
    ) -> JsonObject:
        self.calls.append(("bitable_query_records", (app_id, app_secret, app_token, table_id, filters)))
        return self._responses["bitable_query_records"].popleft()

    async def bitable_create_record(
        self, app_id: str, app_secret: str, app_token: str, table_id: str, fields: dict[str, ToolArgumentValue]
    ) -> JsonObject:
        self.calls.append(("bitable_create_record", (app_id, app_secret, app_token, table_id, fields)))
        return self._responses["bitable_create_record"].popleft()

    async def bitable_update_record(
        self,
        app_id: str,
        app_secret: str,
        app_token: str,
        table_id: str,
        record_id: str,
        fields: dict[str, ToolArgumentValue],
    ) -> JsonObject:
        self.calls.append(("bitable_update_record", (app_id, app_secret, app_token, table_id, record_id, fields)))
        return self._responses["bitable_update_record"].popleft()

    async def bitable_delete_record(
        self, app_id: str, app_secret: str, app_token: str, table_id: str, record_id: str
    ) -> JsonObject:
        self.calls.append(("bitable_delete_record", (app_id, app_secret, app_token, table_id, record_id)))
        return self._responses["bitable_delete_record"].popleft()


def _bitable_module():
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_feishu_bitable")


def _client_module():
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_feishu_client")


def _patch_facade(
    monkeypatch, service: _FakeFeishuService, credentials: tuple[str, str] = ("app-id", "app-secret")
) -> None:
    async def get_credentials(_agent_id: uuid.UUID) -> tuple[str, str]:
        return credentials

    def parse_url(_url: str) -> dict[str, str]:
        return {"app_token": "app_token_1", "table_id": "tblA", "view_id": "vew1"}

    async def resolve_app_token(_agent_id: uuid.UUID, parsed_url: dict[str, str]) -> str | None:
        return parsed_url.get("app_token")

    async def bitable_url(_tenant_token: str, app_token: str, table_id: str = "") -> str:
        if table_id:
            return f"https://tenant.example/base/{app_token}?table={table_id}"
        return f"https://tenant.example/base/{app_token}"

    monkeypatch.setattr(agent_tools, "_get_feishu_credentials", get_credentials)
    monkeypatch.setattr(agent_tools, "_parse_feishu_url", parse_url)
    monkeypatch.setattr(agent_tools, "_resolve_bitable_app_token", resolve_app_token)
    monkeypatch.setattr(agent_tools, "_get_feishu_bitable_url", bitable_url)
    monkeypatch.setattr(importlib.import_module("app.services.feishu_service"), "feishu_service", service)


def test_feishu_client_helpers_parse_urls_and_errors() -> None:
    client = _client_module()

    parsed = client._parse_feishu_url("https://tenant.feishu.cn/base/app_token_1/tblA?table=tblB&view=vew1")

    assert parsed == {"app_token": "app_token_1", "table_id": "tblB", "view_id": "vew1"}
    assert client._parse_feishu_url("https://tenant.feishu.cn/docx/doccn123") == {"document_token": "doccn123"}
    assert client._parse_feishu_url("https://tenant.feishu.cn/wiki/wikcn123") == {"wiki_token": "wikcn123"}
    assert client._check_feishu_err({"code": 0}) is None
    assert (
        client._check_feishu_err({"code": 19000, "msg": "token invalid"}) == "Failed: API Error 19000 - token invalid"
    )
    assert client._check_feishu_err({"code": 99991663, "msg": "no permission to access resource"}) == PERMISSION_MESSAGE


async def _assert_success_shape(
    monkeypatch,
    helper_name: str,
    arguments: registry.ToolArguments,
    responses: dict[str, list[JsonObject]],
    expected: str,
) -> None:
    target = _bitable_module()
    service = _FakeFeishuService(responses)
    _patch_facade(monkeypatch, service)

    result = await getattr(target, helper_name)(uuid.uuid4(), arguments)

    assert result == expected


async def test_bitable_create_app_preserves_success_shape(monkeypatch) -> None:
    responses: dict[str, list[JsonObject]] = {
        "bitable_create_app": [
            {
                "code": 0,
                "data": {"app": {"app_token": "app_token_1", "url": BITABLE_LINK, "default_table_id": "tblA"}},
            }
        ]
    }
    expected = (
        "OK: Bitable created successfully!\nName: Roadmap\nApp Token: app_token_1\n"
        f"URL: {BITABLE_LINK}\nDefault Table ID: tblA"
    )

    await _assert_success_shape(monkeypatch, "_bitable_create_app", {"name": "Roadmap"}, responses, expected)


async def test_bitable_list_tables_preserves_success_shape(monkeypatch) -> None:
    responses: dict[str, list[JsonObject]] = {
        "bitable_list_tables": [{"code": 0, "data": {"items": [{"name": "Table A", "table_id": "tblA"}]}}]
    }
    expected = f"OK: Tables in this Bitable:\n- Table A (ID: tblA)\n\n🔗 多维表格链接: {BITABLE_LINK}"

    await _assert_success_shape(monkeypatch, "_bitable_list_tables", {"url": BITABLE_URL}, responses, expected)


async def test_bitable_list_fields_preserves_success_shape(monkeypatch) -> None:
    responses: dict[str, list[JsonObject]] = {
        "bitable_list_fields": [{"code": 0, "data": {"items": [{"field_name": "Name", "type": 1, "field_id": "fld1"}]}}]
    }

    await _assert_success_shape(
        monkeypatch,
        "_bitable_list_fields",
        {"url": BITABLE_URL},
        responses,
        "OK: Fields in this table:\n- Name (type: 1, ID: fld1)",
    )


async def test_bitable_query_records_preserves_success_shape(monkeypatch) -> None:
    responses: dict[str, list[JsonObject]] = {
        "bitable_query_records": [{"code": 0, "data": {"items": [{"record_id": "rec1", "fields": {"Name": "Alice"}}]}}]
    }
    arguments: registry.ToolArguments = {
        "url": BITABLE_URL,
        "filter_info": json.dumps({"conditions": []}),
        "max_results": 1,
    }

    await _assert_success_shape(
        monkeypatch,
        "_bitable_query_records",
        arguments,
        responses,
        'OK: Query results:\nRecord rec1: {"Name": "Alice"}',
    )


async def test_bitable_create_record_preserves_success_shape(monkeypatch) -> None:
    responses: dict[str, list[JsonObject]] = {
        "bitable_create_record": [
            {"code": 0, "data": {"record": {"record_id": "rec-new", "fields": {"Name": "Alice"}}}}
        ]
    }
    expected = (
        f'OK: Record created. Record ID: rec-new\nFields: {{"Name": "Alice"}}\n🔗 多维表格链接: {BITABLE_TABLE_LINK}'
    )

    await _assert_success_shape(
        monkeypatch,
        "_bitable_create_record",
        {"url": BITABLE_URL, "fields": json.dumps({"Name": "Alice"})},
        responses,
        expected,
    )


async def test_bitable_update_record_preserves_success_shape(monkeypatch) -> None:
    responses: dict[str, list[JsonObject]] = {
        "bitable_update_record": [{"code": 0, "data": {"record": {"record_id": "rec1", "fields": {"Name": "Bob"}}}}]
    }
    expected = f'OK: Record updated. Record ID: rec1\nFields: {{"Name": "Bob"}}\n🔗 多维表格链接: {BITABLE_TABLE_LINK}'

    await _assert_success_shape(
        monkeypatch,
        "_bitable_update_record",
        {"url": BITABLE_URL, "record_id": "rec1", "fields": json.dumps({"Name": "Bob"})},
        responses,
        expected,
    )


async def test_bitable_delete_record_preserves_success_shape(monkeypatch) -> None:
    responses: dict[str, list[JsonObject]] = {"bitable_delete_record": [{"code": 0, "data": {}}]}
    expected = f"OK: Record rec1 deleted successfully.\n🔗 多维表格链接: {BITABLE_TABLE_LINK}"

    await _assert_success_shape(
        monkeypatch,
        "_bitable_delete_record",
        {"url": BITABLE_URL, "record_id": "rec1"},
        responses,
        expected,
    )


async def test_bitable_facade_wrappers_preserve_missing_credentials_and_provider_errors(monkeypatch) -> None:
    _bitable_module()
    missing_service = _FakeFeishuService({})
    _patch_facade(monkeypatch, missing_service, credentials=("", ""))

    missing_result = await agent_tools._bitable_create_app(uuid.uuid4(), {"name": "Roadmap"})

    assert missing_result == "Failed: Feishu app credentials not configured for this agent."

    provider_service = _FakeFeishuService(
        {"bitable_list_tables": [{"code": 99991663, "msg": "no permission to access resource"}]}
    )
    _patch_facade(monkeypatch, provider_service)

    provider_result = await agent_tools._bitable_list_tables(uuid.uuid4(), {"url": BITABLE_URL})

    assert provider_result == PERMISSION_MESSAGE


async def test_bitable_handlers_use_agent_tools_patch_seams(monkeypatch) -> None:
    target = _bitable_module()
    service = _FakeFeishuService({"bitable_list_tables": [{"code": 0, "data": {"items": []}}]})
    _patch_facade(monkeypatch, service)
    seen: list[tuple[str, dict[str, str]]] = []

    async def resolve_app_token(_agent_id: uuid.UUID, parsed_url: dict[str, str]) -> str | None:
        seen.append(("resolve", parsed_url))
        return "patched_app_token"

    monkeypatch.setattr(agent_tools, "_resolve_bitable_app_token", resolve_app_token)

    result = await target._bitable_list_tables(uuid.uuid4(), {"url": "patched-url"})

    assert result == "OK: No tables found in this Bitable."
    assert seen == [("resolve", {"app_token": "app_token_1", "table_id": "tblA", "view_id": "vew1"})]
    assert service.calls == [("bitable_list_tables", ("app-id", "app-secret", "patched_app_token"))]


async def test_registry_bitable_handler_calls_extracted_module_not_legacy_facade(monkeypatch) -> None:
    target = _bitable_module()
    calls: list[tuple[uuid.UUID, registry.ToolArguments]] = []

    async def extracted_create(agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        calls.append((agent_id, arguments))
        return "created through extracted bitable module"

    async def legacy_create(_agent_id: uuid.UUID, _arguments: registry.ToolArguments) -> str:
        raise AssertionError("registered Bitable handler must not defer through agent_tools facade")

    monkeypatch.setattr(target, "_bitable_create_app", extracted_create)
    monkeypatch.setattr(agent_tools, "_bitable_create_app", legacy_create)
    handler = registry.resolve("bitable_create_app")
    assert handler is not None
    agent_id = uuid.uuid4()
    arguments: registry.ToolArguments = {"name": "Roadmap"}

    handler_result = handler(
        arguments=arguments,
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        session_id="session-feishu-bitable",
        on_output=None,
    )
    result = handler_result if isinstance(handler_result, str) else await handler_result

    assert result == "created through extracted bitable module"
    assert calls == [(agent_id, arguments)]
