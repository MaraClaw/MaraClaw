from __future__ import annotations

import importlib
import uuid
from collections import deque
from typing import NotRequired, TypedDict, TypeGuard, Unpack

from app.core.json_types import JsonObject
from app.services import agent_tools
from app.services.agent_tool_exec import channel_context, registry

_FW_COLON = "\uff1a"
_FW_COMMA = "\uff0c"
_FW_EXCLAMATION = "\uff01"
_FW_LEFT_PAREN = "\uff08"
_FW_RIGHT_PAREN = "\uff09"


class _HttpCallKwargs(TypedDict, total=False):
    timeout: int
    headers: JsonObject
    params: JsonObject
    json: JsonObject


type HttpCall = tuple[str, str, _HttpCallKwargs]


class _JsonHttpCallKwargs(TypedDict):
    timeout: int
    json: JsonObject
    headers: NotRequired[JsonObject]
    params: NotRequired[JsonObject]


type _JsonHttpCall = tuple[str, str, _JsonHttpCallKwargs]


def _has_json_payload(call: HttpCall) -> TypeGuard[_JsonHttpCall]:
    return "json" in call[2]


def _json_call_at(calls: list[HttpCall], index: int) -> _JsonHttpCall:
    return next(call for call in calls[index : index + 1] if _has_json_payload(call))


class _Response:
    def __init__(self, payload: JsonObject) -> None:
        self._payload = payload

    def json(self) -> JsonObject:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, responses: deque[JsonObject], calls: list[HttpCall], timeout: int) -> None:
        self._responses = responses
        self._calls = calls
        self._timeout = timeout

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_args) -> bool:
        return False

    async def get(self, url: str, **kwargs: Unpack[_HttpCallKwargs]) -> _Response:
        self._calls.append(("GET", url, {"timeout": self._timeout, **kwargs}))
        return _Response(self._responses.popleft())

    async def post(self, url: str, **kwargs: Unpack[_HttpCallKwargs]) -> _Response:
        self._calls.append(("POST", url, {"timeout": self._timeout, **kwargs}))
        return _Response(self._responses.popleft())


class _FakeHttpxModule:
    def __init__(self, *responses: JsonObject) -> None:
        self.responses = deque(responses)
        self.calls: list[HttpCall] = []
        self.AsyncClient = lambda timeout: _FakeAsyncClient(self.responses, self.calls, timeout)


class _FakeFeishuService:
    def __init__(self, *, create_responses: list[JsonObject] | None = None) -> None:
        self.create_responses = deque(create_responses or [])
        self.create_calls: list[tuple[str, str, str, str]] = []
        self.token_calls: list[tuple[str, str]] = []

    async def get_tenant_access_token(self, app_id: str, app_secret: str) -> str:
        self.token_calls.append((app_id, app_secret))
        return "tenant-token"

    async def create_feishu_doc(self, app_id: str, app_secret: str, folder_token: str, title: str) -> JsonObject:
        self.create_calls.append((app_id, app_secret, folder_token, title))
        return self.create_responses.popleft()


def _docs_module():
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_feishu_docs")


def _write_module():
    return importlib.import_module("app.services.agent_tool_exec.feishu_docs_write")


def _patch_facade(monkeypatch, service: _FakeFeishuService, *, node_info: JsonObject | None = None) -> None:
    async def get_credentials(_agent_id: uuid.UUID) -> tuple[str, str]:
        return "app-id", "app-secret"

    def parse_url(_url: str) -> dict[str, str]:
        return {"document_token": "doc-from-url"}

    async def tenant_doc_url(_tenant_token: str, doc_token: str, doc_type: str = "docx") -> str:
        return f"https://tenant.example/{doc_type}/{doc_token}"

    async def wiki_get_node(_token: str, _auth_token: str) -> JsonObject | None:
        return node_info

    monkeypatch.setattr(agent_tools, "_get_feishu_credentials", get_credentials)
    monkeypatch.setattr(agent_tools, "_parse_feishu_url", parse_url)
    monkeypatch.setattr(agent_tools, "_get_feishu_tenant_doc_url", tenant_doc_url)
    monkeypatch.setattr(agent_tools, "_feishu_wiki_get_node", wiki_get_node)
    monkeypatch.setattr(importlib.import_module("app.services.feishu_service"), "feishu_service", service)


async def test_doc_create_preserves_regular_wiki_and_folder_token_fallback_shapes(monkeypatch) -> None:
    target = _docs_module()
    writer = _write_module()
    service = _FakeFeishuService(create_responses=[{"code": 0, "data": {"document": {"document_id": "doc_new"}}}])
    _patch_facade(monkeypatch, service)
    share_httpx = _FakeHttpxModule({"code": 0, "msg": "ok"})
    monkeypatch.setattr(writer, "_httpx_module", lambda: share_httpx)
    sender_token = channel_context.channel_feishu_sender_open_id.set("ou_sender")
    try:
        regular = await target._feishu_doc_create(uuid.uuid4(), {"title": "Plan", "folder_token": "fld1"})
    finally:
        channel_context.channel_feishu_sender_open_id.reset(sender_token)

    assert f"✅ 文档创建成功{_FW_EXCLAMATION}\n✅ 已自动为你开通访问权限。" in regular
    assert f"Token{_FW_COLON}doc_new" in regular
    assert service.create_calls == [("app-id", "app-secret", "fld1", "Plan")]
    assert _json_call_at(share_httpx.calls, 0)[2]["json"]["member_id"] == "ou_sender"

    wiki_httpx = _FakeHttpxModule({"code": 0, "data": {"node": {"obj_token": "doc_wiki", "node_token": "wiki_node"}}})
    monkeypatch.setattr(writer, "_httpx_module", lambda: wiki_httpx)
    _patch_facade(monkeypatch, _FakeFeishuService(), node_info={"space_id": "space1", "node_token": "parent1"})
    wiki = await target._feishu_doc_create(
        uuid.uuid4(), {"title": "Wiki Plan", "wiki_space_id": "space1", "parent_node_token": "parent1"}
    )
    assert f"✅ 知识库文档创建成功{_FW_EXCLAMATION}" in wiki
    assert f"文档 Token{_FW_LEFT_PAREN}用于 feishu_doc_append{_FW_RIGHT_PAREN}{_FW_COLON}doc_wiki" in wiki
    assert f"Wiki Node Token{_FW_COLON}wiki_node" in wiki

    fallback_httpx = _FakeHttpxModule(
        {"code": 0, "data": {"node": {"obj_token": "doc_fallback", "node_token": "wiki_fallback"}}}
    )
    fallback_service = _FakeFeishuService(create_responses=[])
    monkeypatch.setattr(writer, "_httpx_module", lambda: fallback_httpx)
    _patch_facade(monkeypatch, fallback_service, node_info={"space_id": "space1", "node_token": "parent1"})
    fallback = await target._feishu_doc_create(uuid.uuid4(), {"title": "Fallback", "folder_token": "parent1"})
    assert f"Wiki Node Token{_FW_COLON}wiki_fallback" in fallback
    assert fallback_service.create_calls == []


async def test_doc_append_preserves_markdown_children_and_error_shape(monkeypatch) -> None:
    target = _docs_module()
    writer = _write_module()
    _patch_facade(monkeypatch, _FakeFeishuService())
    httpx = _FakeHttpxModule(
        {"code": 0, "data": {"document": {"body": {"block_id": "body1"}}}},
        {"code": 0, "data": {}},
    )
    monkeypatch.setattr(writer, "_httpx_module", lambda: httpx)

    result = await target._feishu_doc_append(uuid.uuid4(), {"document_token": "doc123", "content": "# Title"})

    assert result == (
        "✅ 已写入 1 个段落到文档。\n"
        f"🔗 文档直链{_FW_LEFT_PAREN}原文发给用户{_FW_COMMA}勿修改{_FW_RIGHT_PAREN}{_FW_COLON}"
        "https://tenant.example/docx/doc123"
    )
    assert httpx.calls[1][1].endswith("/docx/v1/documents/doc123/blocks/body1/children")
    assert _json_call_at(httpx.calls, 1)[2]["json"] == {
        "children": [{"block_type": 3, "heading1": {"elements": [{"text_run": {"content": "Title"}}]}}]
    }

    error_httpx = _FakeHttpxModule({"code": 19000, "msg": "metadata failed"})
    monkeypatch.setattr(writer, "_httpx_module", lambda: error_httpx)
    error = await target._feishu_doc_append(uuid.uuid4(), {"document_token": "doc123", "content": "body"})
    assert error == "Failed: API Error 19000 - metadata failed"


async def test_registry_doc_handler_calls_extracted_module_not_legacy_facade(monkeypatch) -> None:
    target = _docs_module()
    calls: list[tuple[uuid.UUID, registry.ToolArguments]] = []

    async def extracted_search(agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        calls.append((agent_id, arguments))
        return "searched through extracted docs module"

    async def legacy_search(_agent_id: uuid.UUID, _arguments: registry.ToolArguments) -> str:
        raise AssertionError("registered docs handler must not defer through agent_tools facade")

    monkeypatch.setattr(target, "_feishu_doc_search", extracted_search)
    monkeypatch.setattr(agent_tools, "_feishu_doc_search", legacy_search)
    handler = registry.resolve("feishu_doc_search")
    assert handler is not None
    agent_id = uuid.uuid4()
    arguments: registry.ToolArguments = {"query": "roadmap"}

    handler_result = handler(
        arguments=arguments,
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        session_id="session-feishu-docs",
        on_output=None,
    )
    result = handler_result if isinstance(handler_result, str) else await handler_result

    assert result == "searched through extracted docs module"
    assert calls == [(agent_id, arguments)]
