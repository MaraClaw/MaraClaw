from __future__ import annotations

import importlib
import uuid
from typing import Protocol, TypeIs

import httpx

from app.core.json_types import JsonObject, json_as_str, json_as_str_or, json_object_from, json_object_from_response
from app.services import agent_tools
from app.services.agent_tool_exec.channel_context import channel_feishu_sender_open_id
from app.services.agent_tool_exec.registry import ToolArguments, tool_arg_str, tool_arg_str_or

_FW_COLON = "\uff1a"
_FW_COMMA = "\uff0c"
_FW_EXCLAMATION = "\uff01"
_FW_LEFT_PAREN = "\uff08"
_FW_RIGHT_PAREN = "\uff09"


class _FeishuDocsService(Protocol):
    async def get_tenant_access_token(self, app_id: str | None = None, app_secret: str | None = None) -> str: ...

    async def create_feishu_doc(
        self, app_id: str, app_secret: str, folder_token: str | None = None, title: str = "Untitled Document"
    ) -> JsonObject: ...


class _FeishuServiceModule(Protocol):
    feishu_service: _FeishuDocsService


def _is_feishu_service_module(value: object) -> TypeIs[_FeishuServiceModule]:
    service: object = getattr(value, "feishu_service", None)
    return callable(getattr(service, "get_tenant_access_token", None)) and callable(
        getattr(service, "create_feishu_doc", None)
    )


def _feishu_service() -> _FeishuDocsService:
    module: object = importlib.import_module("app.services.feishu_service")
    if not _is_feishu_service_module(module):
        raise TypeError("feishu_service is unavailable")
    return module.feishu_service


def _httpx_module():
    import httpx

    return httpx


def _httpx_client(*args: object, **kwargs: object):
    return _httpx_module().AsyncClient(*args, **kwargs)


def _response_mapping(response: httpx.Response) -> JsonObject:
    return json_object_from_response(response)


async def _feishu_doc_create(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    title_value = arguments.get("title", "")
    title = title_value.strip() if isinstance(title_value, str) else ""
    if not title:
        return "Failed: Missing required argument 'title'"
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    folder_token_value = arguments.get("folder_token", "")
    wiki_space_id_value = arguments.get("wiki_space_id", "")
    parent_node_token_value = arguments.get("parent_node_token", "")
    folder_token = folder_token_value.strip() if isinstance(folder_token_value, str) else ""
    wiki_space_id = wiki_space_id_value.strip() if isinstance(wiki_space_id_value, str) else ""
    parent_node_token = parent_node_token_value.strip() if isinstance(parent_node_token_value, str) else ""
    service = _feishu_service()
    tenant_token = await service.get_tenant_access_token(app_id, app_secret)

    try:
        if folder_token and not wiki_space_id and not parent_node_token:
            probe = await agent_tools._feishu_wiki_get_node(folder_token, tenant_token)
            probe_space_id = tool_arg_str(probe.get("space_id")) if probe else None
            if probe_space_id:
                wiki_space_id = probe_space_id
                parent_node_token = tool_arg_str_or(probe.get("node_token") if probe else None, folder_token)
                folder_token = ""

        if parent_node_token and not wiki_space_id:
            node_info = await agent_tools._feishu_wiki_get_node(parent_node_token, tenant_token)
            node_space_id = tool_arg_str(node_info.get("space_id")) if node_info else None
            if node_space_id:
                wiki_space_id = node_space_id

        if wiki_space_id:
            return await _create_wiki_doc(agent_tools, tenant_token, title, wiki_space_id, parent_node_token)

        response = await service.create_feishu_doc(app_id, app_secret, folder_token, title)
        error = agent_tools._check_feishu_err(response)
        if error:
            return error
        doc_token = json_as_str_or(
            json_object_from(json_object_from(response.get("data")).get("document")).get("document_id")
        )
        doc_url = await agent_tools._get_feishu_tenant_doc_url(tenant_token, doc_token)
        share_note = await _share_with_sender(agent_tools, tenant_token, doc_token)
        return (
            f"✅ 文档创建成功{_FW_EXCLAMATION}{share_note}\n"
            + f"标题{_FW_COLON}{title}\n"
            + f"Token{_FW_COLON}{doc_token}\n"
            + f"🔗 访问链接{_FW_COLON}{doc_url}\n"
            + f'下一步{_FW_COLON}调用 feishu_doc_append(document_token="{doc_token}", content="...") 写入正文内容。'
        )
    except Exception as error:
        return f"Failed: {str(error)[:300]}"


async def _create_wiki_doc(
    facade: object,
    tenant_token: str,
    title: str,
    wiki_space_id: str,
    parent_node_token: str,
) -> str:
    body: dict[str, str] = {"obj_type": "docx", "node_type": "origin", "title": title}
    if parent_node_token:
        body["parent_node_token"] = parent_node_token

    async with _httpx_client(timeout=15) as client:
        response = await client.post(
            f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{wiki_space_id}/nodes",
            json=body,
            headers={"Authorization": f"Bearer {tenant_token}"},
        )
    result = _response_mapping(response)
    error = agent_tools._check_feishu_err(result)
    if error:
        return error

    node = json_object_from(json_object_from(result.get("data")).get("node"))
    doc_token = json_as_str_or(node.get("obj_token"))
    node_token = json_as_str_or(node.get("node_token"))
    doc_url = await agent_tools._get_feishu_tenant_doc_url(tenant_token, node_token, doc_type="wiki")
    return (
        f"✅ 知识库文档创建成功{_FW_EXCLAMATION}\n"
        + f"标题{_FW_COLON}{title}\n"
        + f"文档 Token{_FW_LEFT_PAREN}用于 feishu_doc_append{_FW_RIGHT_PAREN}{_FW_COLON}{doc_token}\n"
        + f"Wiki Node Token{_FW_COLON}{node_token}\n"
        + f"🔗 访问链接{_FW_COLON}{doc_url}\n"
        + f'下一步{_FW_COLON}调用 feishu_doc_append(document_token="{doc_token}", content="...") 写入正文内容。'
    )


async def _share_with_sender(facade: object, tenant_token: str, doc_token: str) -> str:
    try:
        sender_open_id = channel_feishu_sender_open_id.get(None)
        if not sender_open_id or not doc_token:
            return ""
        async with _httpx_client(timeout=10) as client:
            response = await client.post(
                f"https://open.feishu.cn/open-apis/drive/v1/permissions/{doc_token}/members",
                params={"type": "docx"},
                json={"member_type": "openid", "member_id": sender_open_id, "perm": "full_access"},
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
        result = _response_mapping(response)
        if result.get("code") == 0:
            return "\n✅ 已自动为你开通访问权限。"
        return f"\n⚠️ 自动授权失败{_FW_LEFT_PAREN}{result.get('code')}{_FW_RIGHT_PAREN}{_FW_COMMA}你可能需要手动在飞书前端搜索此文件。"
    except Exception as error:
        return f"\n⚠️ 自动授权异常: {error}"


async def _feishu_doc_append(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    document_token_value = arguments.get("document_token", "")
    document_token = document_token_value.strip() if isinstance(document_token_value, str) else ""
    if not document_token:
        url_value = arguments.get("url", "")
        url = url_value if isinstance(url_value, str) else ""
        parsed = agent_tools._parse_feishu_url(url)
        document_token = parsed.get("document_token", parsed.get("wiki_token", ""))

    content_value = arguments.get("content", "")
    content = content_value.strip() if isinstance(content_value, str) else ""
    if not document_token:
        return "Failed: Missing required argument 'document_token'"
    if not content:
        return "Failed: Missing required argument 'content'"

    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    tenant_token = await _feishu_service().get_tenant_access_token(app_id, app_secret)
    node_info = await agent_tools._feishu_wiki_get_node(document_token, tenant_token)
    obj_token = tool_arg_str(node_info.get("obj_token")) if node_info else None
    docx_token = obj_token or document_token

    try:
        async with _httpx_client(timeout=20) as client:
            metadata_response = await client.get(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{docx_token}",
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            metadata = _response_mapping(metadata_response)
            error = agent_tools._check_feishu_err(metadata)
            if error:
                return error

            body = json_object_from(
                json_object_from(json_object_from(metadata.get("data")).get("document")).get("body")
            )
            body_block_id = json_as_str(body.get("block_id")) or docx_token
            children = agent_tools._markdown_to_feishu_blocks(content)
            result_response = await client.post(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{docx_token}/blocks/{body_block_id}/children",
                json={"children": children},
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            result = _response_mapping(result_response)
            error = agent_tools._check_feishu_err(result)
            if error:
                return error

        doc_url = await agent_tools._get_feishu_tenant_doc_url(tenant_token, docx_token)
        return (
            f"✅ 已写入 {len(children)} 个段落到文档。\n"
            + f"🔗 文档直链{_FW_LEFT_PAREN}原文发给用户{_FW_COMMA}勿修改{_FW_RIGHT_PAREN}{_FW_COLON}{doc_url}"
        )
    except Exception as error:
        return f"Failed: {str(error)[:300]}"
