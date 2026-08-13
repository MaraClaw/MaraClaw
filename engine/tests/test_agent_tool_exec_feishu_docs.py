from __future__ import annotations

import importlib
import uuid
from collections import deque

from app.core.json_types import JsonObject
from app.services import agent_tools

_FW_COLON = "\uff1a"
_FW_COMMA = "\uff0c"
_FW_LEFT_PAREN = "\uff08"
_FW_RIGHT_PAREN = "\uff09"


class _Response:
    def __init__(self, payload: JsonObject) -> None:
        self._payload = payload

    def json(self) -> JsonObject:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, responses: deque[JsonObject], calls: list[tuple[str, str, JsonObject]], timeout: int) -> None:
        self._responses = responses
        self._calls = calls
        self._timeout = timeout

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_args) -> bool:
        return False

    async def get(self, url: str, **kwargs) -> _Response:
        self._calls.append(("GET", url, {"timeout": self._timeout, **kwargs}))
        return _Response(self._responses.popleft())

    async def post(self, url: str, **kwargs) -> _Response:
        self._calls.append(("POST", url, {"timeout": self._timeout, **kwargs}))
        return _Response(self._responses.popleft())


class _FakeHttpxModule:
    def __init__(self, *responses: JsonObject) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, str, JsonObject]] = []
        self.AsyncClient = lambda timeout: _FakeAsyncClient(self.responses, self.calls, timeout)


class _FakeFeishuService:
    def __init__(self, *, read_responses: list[JsonObject] | None = None) -> None:
        self.read_responses = deque(read_responses or [])
        self.token_calls: list[tuple[str, str]] = []
        self.read_calls: list[tuple[str, str, str]] = []

    async def get_tenant_access_token(self, app_id: str, app_secret: str) -> str:
        self.token_calls.append((app_id, app_secret))
        return "tenant-token"

    async def read_feishu_doc(self, app_id: str, app_secret: str, document_token: str) -> JsonObject:
        self.read_calls.append((app_id, app_secret, document_token))
        return self.read_responses.popleft()


def _docs_module():
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_feishu_docs")


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


async def test_doc_search_preserves_success_shape(monkeypatch) -> None:
    target = _docs_module()
    service = _FakeFeishuService()
    _patch_facade(monkeypatch, service)
    httpx = _FakeHttpxModule(
        {
            "code": 0,
            "data": {
                "docs_entities": [
                    {"title": "Roadmap", "docs_token": "doc123", "docs_type": "docx", "owner_id": "owner1"}
                ],
                "total": 2,
                "has_more": True,
            },
        }
    )
    monkeypatch.setattr(target, "_httpx_module", lambda: httpx)

    result = await target._feishu_doc_search(uuid.uuid4(), {"query": "roadmap"})

    assert f"🔎 飞书文档搜索结果{_FW_COLON}关键词 `roadmap`" in result
    assert "1. **Roadmap**" in result
    assert "docs_token: `doc123`" in result
    assert f'下一页{_FW_COLON}`feishu_doc_search(query="roadmap", offset=1, count=10)`' in result
    assert httpx.calls[0][2]["json"] == {"search_key": "roadmap", "count": 10, "offset": 0}


async def test_doc_search_preserves_no_result_and_provider_error_shapes(monkeypatch) -> None:
    target = _docs_module()
    _patch_facade(monkeypatch, _FakeFeishuService())
    empty_httpx = _FakeHttpxModule({"code": 0, "data": {"docs_entities": [], "total": 0, "has_more": False}})
    monkeypatch.setattr(target, "_httpx_module", lambda: empty_httpx)

    empty_result = await target._feishu_doc_search(uuid.uuid4(), {"query": "missing"})

    assert "🔎 未找到与 `missing` 匹配的飞书文档。" in empty_result
    assert f"指定 docs_types 过滤{_FW_COMMA}例如 ['docx'] 或 ['bitable']" in empty_result

    error_httpx = _FakeHttpxModule({"code": 19000, "msg": "bad search"})
    monkeypatch.setattr(target, "_httpx_module", lambda: error_httpx)

    error_result = await target._feishu_doc_search(uuid.uuid4(), {"query": "boom"})

    assert error_result == "Failed: API Error 19000 - bad search"


async def test_wiki_list_preserves_success_unresolved_and_empty_shapes(monkeypatch) -> None:
    target = _docs_module()
    service = _FakeFeishuService()
    _patch_facade(monkeypatch, service, node_info={"space_id": "space1", "obj_token": "doc_node"})
    httpx = _FakeHttpxModule(
        {
            "code": 0,
            "data": {
                "items": [{"title": "Child", "node_token": "child1", "obj_token": "doc_child", "has_child": True}]
            },
        }
    )
    monkeypatch.setattr(target, "_httpx_module", lambda: httpx)

    result = await target._feishu_wiki_list(uuid.uuid4(), {"node_token": "node1"})

    assert f"📂 Wiki 页面 `node1` 的子页面{_FW_LEFT_PAREN}共 1 个{_FW_RIGHT_PAREN}{_FW_COLON}" in result
    assert "space_id: `space1`" in result
    assert "• **Child** _(有子页面)_" in result

    _patch_facade(monkeypatch, service, node_info=None)
    unresolved = await target._feishu_wiki_list(uuid.uuid4(), {"node_token": "bad-node"})
    assert "❌ 无法解析 Wiki 节点 `bad-node`。" in unresolved

    _patch_facade(monkeypatch, service, node_info={"space_id": "space1", "obj_token": "doc_node"})
    empty_httpx = _FakeHttpxModule({"code": 0, "data": {"items": []}})
    monkeypatch.setattr(target, "_httpx_module", lambda: empty_httpx)
    empty = await target._feishu_wiki_list(uuid.uuid4(), {"node_token": "node1"})
    assert empty == "📂 Wiki 页面 `node1` 下没有子页面。"


async def test_doc_read_preserves_regular_wiki_empty_and_error_shapes(monkeypatch) -> None:
    target = _docs_module()
    service = _FakeFeishuService(read_responses=[{"code": 0, "data": {"content": "Hello"}}])
    _patch_facade(monkeypatch, service)

    regular = await target._feishu_doc_read(uuid.uuid4(), {"document_token": "doc123"})

    assert regular == "📄 **Document content** (`doc123`):\n\nHello"
    assert service.read_calls == [("app-id", "app-secret", "doc123")]

    wiki_service = _FakeFeishuService(read_responses=[{"code": 0, "data": {"content": "Wiki body"}}])
    _patch_facade(monkeypatch, wiki_service, node_info={"obj_token": "doc_wiki", "has_child": True})
    wiki = await target._feishu_doc_read(uuid.uuid4(), {"document_token": "wiki123"})
    assert "📄 **Document content** (`wiki123`):\n\nWiki body" in wiki
    assert "这是一个 Wiki 目录页" in wiki
    assert wiki_service.read_calls == [("app-id", "app-secret", "doc_wiki")]

    empty_service = _FakeFeishuService(read_responses=[{"code": 0, "data": {"content": ""}}])
    _patch_facade(monkeypatch, empty_service)
    empty = await target._feishu_doc_read(uuid.uuid4(), {"document_token": "doc123"})
    assert empty == "📄 Document 'doc123' is empty."

    error_service = _FakeFeishuService(read_responses=[{"code": 19000, "msg": "read failed"}])
    _patch_facade(monkeypatch, error_service)
    error = await target._feishu_doc_read(uuid.uuid4(), {"document_token": "doc123"})
    assert error == "Failed: API Error 19000 - read failed"
