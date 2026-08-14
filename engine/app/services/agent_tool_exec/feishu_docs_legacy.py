from __future__ import annotations

import importlib
import uuid

from app.services import agent_tools
from app.services.agent_tool_exec.registry import ToolArguments, tool_arg_str


def _feishu_service():
    return importlib.import_module("app.services.feishu_service").feishu_service


async def _resolve_docx_document_token(agent_id: uuid.UUID, parsed_url: dict[str, str]) -> str | None:
    doc_token = parsed_url.get("document_token")
    if doc_token:
        return doc_token
    wiki_token = parsed_url.get("wiki_token")
    if wiki_token:
        app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
        if app_id and app_secret:
            tenant_token = await _feishu_service().get_tenant_access_token(app_id, app_secret)
            node_info = await agent_tools._feishu_wiki_get_node(wiki_token, tenant_token)
            obj_token = tool_arg_str(node_info.get("obj_token")) if node_info else None
            if obj_token:
                return obj_token
    return None


async def _feishu_read_doc(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    url_value = arguments.get("url", "")
    url = url_value if isinstance(url_value, str) else ""
    doc_token = await agent_tools._resolve_docx_document_token(agent_id, agent_tools._parse_feishu_url(url))
    if not doc_token:
        return "Failed: Could not extract Document token from the URL."

    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    try:
        response = await _feishu_service().read_feishu_doc(app_id, app_secret, doc_token)
        error = agent_tools._check_feishu_err(response)
        if error:
            return error
        content = response.get("data", {}).get("content", "")
        if not content:
            return "OK: Document is empty or content is unavailable."
        return f"OK: Document Content:\n{content}"
    except Exception as error:
        return f"Failed: {str(error)[:300]}"


async def _feishu_create_doc(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    title_value = arguments.get("title", "Untitled Document")
    folder_token_value = arguments.get("folder_token", "")
    title = title_value if isinstance(title_value, str) else "Untitled Document"
    folder_token = folder_token_value if isinstance(folder_token_value, str) else ""
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    try:
        service = _feishu_service()
        response = await service.create_feishu_doc(app_id, app_secret, folder_token or None, title)
        error = agent_tools._check_feishu_err(response)
        if error:
            return error
        doc_id = response.get("data", {}).get("document", {}).get("document_id")
        tenant_token = await service.get_tenant_access_token(app_id, app_secret)
        url = await agent_tools._get_feishu_tenant_doc_url(tenant_token, doc_id)
        return f"OK: Document created perfectly. Document ID: {doc_id}\nURL: {url}"
    except Exception as error:
        return f"Failed: {str(error)[:300]}"


async def _feishu_append_doc(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    content_value = arguments.get("content", "")
    content = content_value if isinstance(content_value, str) else ""
    if not content:
        return "Failed: Content to append cannot be empty."
    url_value = arguments.get("url", "")
    url = url_value if isinstance(url_value, str) else ""
    doc_token = await agent_tools._resolve_docx_document_token(agent_id, agent_tools._parse_feishu_url(url))
    if not doc_token:
        return "Failed: Could not extract Document token from the URL."

    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    try:
        response = await _feishu_service().append_feishu_doc(app_id, app_secret, doc_token, content)
        error = agent_tools._check_feishu_err(response)
        if error:
            return error
        return "OK: Content appended successfully to the end of the document."
    except Exception as error:
        return f"Failed: {str(error)[:300]}"
