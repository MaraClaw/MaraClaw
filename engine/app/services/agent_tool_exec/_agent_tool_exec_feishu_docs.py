from __future__ import annotations

import importlib
import uuid
from typing import TypedDict

from app.services import agent_tools

from . import feishu_docs_legacy as _legacy, feishu_docs_write as _write
from .registry import ToolArguments, ToolArgumentValue, tool_arg_str


class _WikiPage(TypedDict):
    title: str
    node_token: str
    obj_token: str
    has_child: bool
    depth: int


_FW_COLON = "\uff1a"
_FW_COMMA = "\uff0c"
_FW_LEFT_PAREN = "\uff08"
_FW_RIGHT_PAREN = "\uff09"


def _feishu_service():
    return importlib.import_module("app.services.feishu_service").feishu_service


def _httpx_module():
    return importlib.import_module("httpx")


def _string_argument(arguments: ToolArguments, name: str, default: str = "") -> str:
    value = arguments.get(name)
    return value if isinstance(value, str) else default


def _integer_argument(arguments: ToolArguments, name: str, default: int) -> int:
    value = arguments.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


async def _resolve_docx_document_token(agent_id: uuid.UUID, parsed_url: dict[str, str]) -> str | None:
    return await _legacy._resolve_docx_document_token(agent_id, parsed_url)


async def _feishu_read_doc(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    return await _legacy._feishu_read_doc(agent_id, arguments)


async def _feishu_create_doc(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    return await _legacy._feishu_create_doc(agent_id, arguments)


async def _feishu_append_doc(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    return await _legacy._feishu_append_doc(agent_id, arguments)


async def _feishu_doc_create(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    return await _write._feishu_doc_create(agent_id, arguments)


async def _feishu_doc_append(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    return await _write._feishu_doc_append(agent_id, arguments)


async def _feishu_wiki_get_node(token_str: str, auth_token: str) -> dict[str, ToolArgumentValue] | None:
    async with _httpx_module().AsyncClient(timeout=5) as client:
        response = await client.get(
            "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node",
            headers={"Authorization": f"Bearer {auth_token}"},
            params={"token": token_str, "obj_type": "wiki"},
        )
    data = response.json()
    if data.get("code") != 0:
        return None
    node = data.get("data", {}).get("node", {})
    return {
        "obj_token": node.get("obj_token", ""),
        "space_id": node.get("origin_space_id", node.get("space_id", "")),
        "has_child": node.get("has_child", False),
        "title": node.get("title", ""),
        "node_token": node.get("node_token", token_str),
    }


async def _feishu_doc_search(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    query = _string_argument(arguments, "query") or _string_argument(arguments, "search_key")
    query = query.strip()
    if not query:
        return "❌ Missing required argument 'query'"

    count = max(1, min(_integer_argument(arguments, "count", 10), 50))
    offset = max(0, _integer_argument(arguments, "offset", 0))
    docs_types = arguments.get("docs_types") or []
    if docs_types and not isinstance(docs_types, list):
        return "❌ 'docs_types' must be an array of strings."
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."

    token = await _feishu_service().get_tenant_access_token(app_id, app_secret)
    payload: dict[str, ToolArgumentValue] = {"search_key": query, "count": count, "offset": offset}
    if docs_types:
        payload["docs_types"] = docs_types

    async with _httpx_module().AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://open.feishu.cn/open-apis/suite/docs-api/search/object",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
    data: dict[str, ToolArgumentValue] = response.json()
    error = agent_tools._check_feishu_err(data)
    if error:
        return error

    result_raw = data.get("data", {})
    result: dict[str, ToolArgumentValue] = result_raw if isinstance(result_raw, dict) else {}
    entities_raw = result.get("docs_entities", []) or []
    entities: list[dict[str, ToolArgumentValue]] = (
        [item for item in entities_raw if isinstance(item, dict)] if isinstance(entities_raw, list) else []
    )
    if not entities:
        return _empty_search_result(query)
    return _format_search_results(query, count, offset, entities, result)


def _empty_search_result(query: str) -> str:
    return (
        f"🔎 未找到与 `{query}` 匹配的飞书文档。"
        + f"\n可以尝试{_FW_COLON}"
        + "\n1. 缩短关键词"
        + "\n2. 换同义词"
        + f"\n3. 指定 docs_types 过滤{_FW_COMMA}例如 ['docx'] 或 ['bitable']"
    )


def _format_search_results(
    query: str,
    count: int,
    offset: int,
    entities: list[dict[str, ToolArgumentValue]],
    result: dict[str, ToolArgumentValue],
) -> str:
    total = result.get("total", len(entities))
    has_more = bool(result.get("has_more", False))
    lines = [
        f"🔎 飞书文档搜索结果{_FW_COLON}关键词 `{query}`",
        f"返回 {len(entities)} 条{_FW_COMMA}total={total}{_FW_COMMA}offset={offset}{_FW_COMMA}has_more={str(has_more).lower()}",
        "",
    ]
    for index, item in enumerate(entities, start=offset + 1):
        lines.append(
            f"{index}. **{item.get('title') or '(无标题)'}**\n"
            + f"   - docs_type: `{item.get('docs_type') or 'unknown'}`\n"
            + f"   - docs_token: `{item.get('docs_token') or ''}`\n"
            + f"   - owner_id: `{item.get('owner_id') or ''}`"
        )
    lines.extend(
        [
            "",
            f"💡 后续操作建议{_FW_COLON}",
            f'- 读取普通文档/知识库页{_FW_COLON}`feishu_doc_read(document_token="...")`',
            f'- 管理权限{_FW_COLON}`feishu_drive_share(document_token="...", doc_type="...", action="list|add|remove")`',
            f'- 删除文件{_FW_COLON}`feishu_drive_delete(file_token="...", file_type="...")`',
        ]
    )
    if has_more:
        lines.append(
            f'- 下一页{_FW_COLON}`feishu_doc_search(query="{query}", offset={offset + len(entities)}, count={count})`'
        )
    return "\n".join(lines)


async def _feishu_wiki_list(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    node_token = _string_argument(arguments, "node_token").strip()
    recursive = bool(arguments.get("recursive", False))
    if not node_token:
        return "❌ Missing required argument 'node_token'"
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."
    tenant_token = await _feishu_service().get_tenant_access_token(app_id, app_secret)
    node_info = await agent_tools._feishu_wiki_get_node(node_token, tenant_token)
    if not node_info:
        return (
            f"❌ 无法解析 Wiki 节点 `{node_token}`。\n请确认 token 来自飞书知识库 URL"
            + f"{_FW_LEFT_PAREN}https://xxx.feishu.cn/wiki/NodeToken{_FW_RIGHT_PAREN}{_FW_COMMA}而非普通文档 URL。"
        )
    space_id = tool_arg_str(node_info.get("space_id"))
    if not space_id:
        return f"❌ 无法获取知识库 space_id{_FW_COMMA}请检查 token 是否正确。"

    pages = await _list_wiki_children(space_id, node_token, tenant_token, recursive, 0)
    if not pages:
        return f"📂 Wiki 页面 `{node_token}` 下没有子页面。"

    lines = [
        f"📂 Wiki 页面 `{node_token}` 的子页面{_FW_LEFT_PAREN}共 {len(pages)} 个{_FW_RIGHT_PAREN}{_FW_COLON}\n"
        + f"space_id: `{space_id}`\n"
    ]
    for page in pages:
        indent = "  " * page["depth"]
        child_hint = " _(有子页面)_" if page["has_child"] else ""
        lines.append(
            f"{indent}• **{page['title']}**{child_hint}\n"
            + f"{indent}  node_token: `{page['node_token']}`\n"
            + f"{indent}  obj_token: `{page['obj_token']}`"
        )
    lines.append(
        '\n💡 用 `feishu_doc_read(document_token="<node_token>")` 读取每个子页面的内容。'
        + f'\n   对有子页面的条目{_FW_COMMA}再次调用 `feishu_wiki_list(node_token="...")` 继续展开。'
    )
    return "\n".join(lines)


async def _list_wiki_children(
    space_id: str, parent_token: str, tenant_token: str, recursive: bool, depth: int
) -> list[_WikiPage]:
    async with _httpx_module().AsyncClient(timeout=15) as client:
        response = await client.get(
            f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/nodes",
            headers={"Authorization": f"Bearer {tenant_token}"},
            params={"parent_node_token": parent_token, "page_size": 50},
        )
    data = response.json()
    if data.get("code") != 0:
        return []
    pages: list[_WikiPage] = []
    for item in data.get("data", {}).get("items", []):
        page: _WikiPage = {
            "title": item.get("title", "(无标题)"),
            "node_token": item.get("node_token", ""),
            "obj_token": item.get("obj_token", ""),
            "has_child": item.get("has_child", False),
            "depth": depth,
        }
        pages.append(page)
        if recursive and page["has_child"] and depth < 2:
            pages.extend(await _list_wiki_children(space_id, page["node_token"], tenant_token, recursive, depth + 1))
    return pages


async def _feishu_doc_read(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    document_token = _string_argument(arguments, "document_token").strip()
    if not document_token:
        parsed = agent_tools._parse_feishu_url(_string_argument(arguments, "url"))
        document_token = parsed.get("document_token", parsed.get("wiki_token", ""))
    if not document_token:
        return "Failed: Missing required argument 'document_token'"

    max_chars = min(_integer_argument(arguments, "max_chars", 6000), 20000)
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."
    tenant_token = await _feishu_service().get_tenant_access_token(app_id, app_secret)

    read_token = document_token
    wiki_hint = ""
    node_info = await agent_tools._feishu_wiki_get_node(document_token, tenant_token)
    if node_info and node_info.get("obj_token"):
        read_token = node_info["obj_token"]
        if node_info.get("has_child"):
            wiki_hint = (
                f"\n\n> 💡 这是一个 Wiki 目录页{_FW_COMMA}它有多个子页面。"
                + f"使用 `feishu_wiki_list` 工具{_FW_LEFT_PAREN}传入相同的 node_token{_FW_RIGHT_PAREN}可以查看所有子页面列表。"
            )

    try:
        response = await _feishu_service().read_feishu_doc(app_id, app_secret, read_token)
        error = agent_tools._check_feishu_err(response)
        if error:
            return error
        content = response.get("data", {}).get("content", "")
        if not content:
            return f"📄 Document '{document_token}' is empty.{wiki_hint}"
        truncated = ""
        if len(content) > max_chars:
            content = content[:max_chars]
            truncated = f"\n\n_(Truncated to {max_chars} chars)_"
        return f"📄 **Document content** (`{document_token}`):\n\n{content}{truncated}{wiki_hint}"
    except Exception as error:
        return f"Failed: {str(error)[:300]}"
