from __future__ import annotations

import re
import uuid
from typing import Final

from httpx import AsyncClient, Response

from app.core.json_types import JsonObject, json_as_str, json_object_from_response
from app.services import agent_tools
from app.services.feishu_service import FeishuService, feishu_service

from .registry import ToolArguments, tool_arg_str_or

_VALID_FILE_TYPES: Final = {"file", "docx", "bitable", "folder", "doc", "sheet", "mindnote", "shortcut", "slides"}
_TYPE_LABELS: Final = {
    "file": "文件",
    "docx": "文档",
    "bitable": "多维表格",
    "folder": "文件夹",
    "doc": "旧版文档",
    "sheet": "电子表格",
    "mindnote": "思维笔记",
    "shortcut": "快捷方式",
    "slides": "幻灯片",
}


def _feishu_service() -> FeishuService:
    return feishu_service


def _httpx_client(*, timeout: float = 5.0, follow_redirects: bool = False) -> AsyncClient:
    return AsyncClient(timeout=timeout, follow_redirects=follow_redirects)


def _response_mapping(response: Response) -> JsonObject:
    return json_object_from_response(response)


def _nested_mapping(value: object) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _object_items(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_argument(arguments: ToolArguments, name: str, default: str = "") -> str:
    value = arguments.get(name)
    return value if isinstance(value, str) else default


def _string_list_argument(arguments: ToolArguments, name: str) -> list[str]:
    value = arguments.get(name)
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


async def _feishu_drive_share(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    document_token = _string_argument(arguments, "document_token").strip()
    doc_type = _string_argument(arguments, "doc_type", "docx").strip()
    action = _string_argument(arguments, "action", "list").strip()
    permission = _string_argument(arguments, "permission", "edit").strip()

    if not document_token:
        return "❌ Missing required argument 'document_token'"
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."
    token = await _feishu_service().get_tenant_access_token(app_id, app_secret)
    headers: dict[str, str] = {"Authorization": f"Bearer {token}"}

    node_info = await agent_tools._feishu_wiki_get_node(document_token, token)
    is_wiki = node_info is not None
    space_id = tool_arg_str_or(node_info.get("space_id")) if node_info else ""
    obj_token = tool_arg_str_or(node_info.get("obj_token")) if node_info else ""

    api_perm = {"view": "view", "edit": "edit", "full_access": "full_access"}.get(permission, "edit")
    wiki_role = "admin" if api_perm in ("edit", "full_access") else "member"

    if action == "list":
        return await _list_drive_members(document_token, obj_token, doc_type, headers, is_wiki, space_id)

    member_names = _string_list_argument(arguments, "member_names")
    member_open_ids = _string_list_argument(arguments, "member_open_ids")
    if not member_names and not member_open_ids:
        return "❌ 请提供 member_names（姓名列表）或 member_open_ids（open_id 列表）"

    resolved: list[tuple[str, str]] = []
    for name in member_names:
        search_result = await agent_tools._feishu_user_search(agent_id, {"name": name})
        match = re.search(r"open_id: `(ou_[A-Za-z0-9]+)`", search_result)
        resolved.append((name, match.group(1) if match else ""))
    resolved.extend((open_id, open_id) for open_id in member_open_ids if open_id)

    results = []
    async with _httpx_client(timeout=15) as client:
        for display, open_id in resolved:
            if not open_id:
                results.append(f"❌ 无法找到「{display}」的 open_id，跳过")
                continue
            if action == "add":
                result = await _add_member(
                    client,
                    display,
                    open_id,
                    document_token,
                    doc_type,
                    permission,
                    api_perm,
                    headers,
                    is_wiki,
                    space_id,
                    wiki_role,
                )
                if result.startswith("❌ 权限不足"):
                    return result
                results.append(result)
            elif action == "remove":
                results.append(
                    await _remove_member(client, display, open_id, document_token, doc_type, headers, is_wiki, space_id)
                )
    return "\n".join(results) if results else "没有需要处理的成员"


async def _list_drive_members(
    document_token: str,
    obj_token: str,
    doc_type: str,
    headers: dict[str, str],
    is_wiki: bool,
    space_id: str,
) -> str:
    use_token = obj_token if (is_wiki and obj_token) else document_token
    async with _httpx_client(timeout=15) as client:
        response = await client.get(
            f"https://open.feishu.cn/open-apis/drive/v1/permissions/{use_token}/members",
            params={"type": doc_type},
            headers=headers,
        )
    data = _response_mapping(response)
    if data.get("code") != 0:
        code = data.get("code")
        if code == 1063003 and is_wiki:
            return (
                f"ℹ️ 文档 `{document_token}` 是知识库页面，其权限由知识库空间统一管理。\n"  # noqa: RUF001
                + "知识库空间 ID：`"
                + space_id
                + "`\n"
                + "请直接在飞书知识库中管理成员权限。"
            )
        if code in (99991672, 99991668):
            return f"❌ 权限不足（code {code}）\n需要在飞书开放平台开通：\n• drive:drive（云文档权限管理）"
        return f"❌ 获取协作者列表失败：{data.get('msg')} (code {code})"

    members = _object_items(_nested_mapping(data.get("data")).get("items"))
    if not members:
        return f"📄 文档 `{document_token}` 当前没有其他协作者。"
    lines = [f"📄 文档 `{document_token}` 的协作者列表（共 {len(members)} 人）：\n"]
    for member in members:
        perm = member.get("perm", "")
        member_type = json_as_str(member.get("member_type")) or ""
        member_id = member.get("member_id", "")
        type_label = {"openid": "用户", "openchat": "群组", "opendepartmentid": "部门"}.get(member_type, member_type)
        lines.append(f"• {type_label} `{member_id}` | 权限: **{perm}**")
    return "\n".join(lines)


async def _add_member(
    client: AsyncClient,
    display: str,
    open_id: str,
    document_token: str,
    doc_type: str,
    permission: str,
    api_perm: str,
    headers: dict[str, str],
    is_wiki: bool,
    space_id: str,
    wiki_role: str,
) -> str:
    if is_wiki and space_id:
        response = await client.post(
            f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/members",
            json={"member_type": "openid", "member_id": open_id, "member_role": wiki_role},
            headers=headers,
        )
        data = _response_mapping(response)
        code = data.get("code")
        if code == 0:
            return f"✅ 已将「{display}」加入知识库空间（角色：{wiki_role}）"
        if code == 131008:
            return f"ℹ️ 「{display}」已经是知识库成员，无需重复添加"  # noqa: RUF001
        if code == 131101:
            return f"ℹ️ 这是一个**公开知识库**，所有人已可访问。\n「{display}」无需单独添加权限。"  # noqa: RUF001
        return f"❌ 添加「{display}」到知识库失败：{data.get('msg')} (code {code})"

    response = await client.post(
        f"https://open.feishu.cn/open-apis/drive/v1/permissions/{document_token}/members",
        json={"member_type": "openid", "member_id": open_id, "perm": api_perm},
        headers=headers,
        params={"type": doc_type},
    )
    data = _response_mapping(response)
    if data.get("code") == 0:
        return f"✅ 已将「{display}」添加为**{permission}**权限协作者"
    code = data.get("code")
    if code == 99992402:
        return "⚠️ 飞书平台安全限制：无法通过 API 为自己添加协作权限。\n请手动操作：打开文档 → 右上角「分享」→ 添加自己并设置权限。"
    if code in (99991672, 99991668):
        return f"❌ 权限不足（code {code}）\n需要在飞书开放平台开通：\n• drive:drive（云文档权限管理）"
    return f"❌ 添加「{display}」失败：{data.get('msg')} (code {code})"


async def _remove_member(
    client: AsyncClient,
    display: str,
    open_id: str,
    document_token: str,
    doc_type: str,
    headers: dict[str, str],
    is_wiki: bool,
    space_id: str,
) -> str:
    if is_wiki and space_id:
        response = await client.delete(
            f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/members/{open_id}",
            headers=headers,
            params={"member_type": "openid"},
        )
        data = _response_mapping(response)
        if data.get("code") == 0:
            return f"✅ 已将「{display}」从知识库移除"
        return f"❌ 移除「{display}」失败：{data.get('msg')} (code {data.get('code')})"

    response = await client.delete(
        f"https://open.feishu.cn/open-apis/drive/v1/permissions/{document_token}/members/{open_id}",
        headers=headers,
        params={"type": doc_type, "member_type": "openid"},
    )
    data = _response_mapping(response)
    if data.get("code") == 0:
        return f"✅ 已移除「{display}」的协作权限"
    return f"❌ 移除「{display}」失败：{data.get('msg')} (code {data.get('code')})"


async def _feishu_drive_delete(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    file_token = _string_argument(arguments, "file_token").strip()
    file_type = _string_argument(arguments, "file_type").strip()

    if not file_token:
        return "❌ Missing required argument 'file_token'"
    if not file_type:
        return "❌ Missing required argument 'file_type'. Valid values: file, docx, bitable, folder, doc, sheet, mindnote, shortcut, slides"
    if file_type not in _VALID_FILE_TYPES:
        return f"❌ Invalid file_type '{file_type}'. Valid values: {', '.join(sorted(_VALID_FILE_TYPES))}"
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."
    token = await _feishu_service().get_tenant_access_token(app_id, app_secret)
    type_label = _TYPE_LABELS.get(file_type, file_type)

    try:
        async with _httpx_client(timeout=15) as client:
            response = await client.delete(
                f"https://open.feishu.cn/open-apis/drive/v1/files/{file_token}",
                params={"type": file_type},
                headers={"Authorization": f"Bearer {token}"},
            )
        data = _response_mapping(response)
        code = data.get("code", -1)
        if code == 0:
            task_id = _nested_mapping(data.get("data")).get("task_id")
            if task_id:
                return f"✅ 已提交{type_label}删除任务（异步执行中）。\n📋 任务 ID: `{task_id}`\n文件夹删除为异步操作，文件会被移至回收站。"
            return f"✅ {type_label} `{file_token}` 已删除（移至回收站）。"

        msg = data.get("msg", "Unknown error")
        if code == 1061003:
            return f"❌ 未找到文件 `{file_token}`。请确认文件 token 和类型是否正确。"
        if code == 1061004:
            return (
                f"❌ 权限不足（code {code}）\n"
                + "需要满足以下条件之一：\n"
                + "• 文件所有者 + 父文件夹编辑权限\n"
                + "• 父文件夹的所有者或 full_access 权限\n"
                + "同时需要在飞书开放平台开通：drive:drive 或 space:document:delete"
            )
        if code == 1061007:
            return f"❌ 文件 `{file_token}` 已被删除。"
        if code == 1061045:
            return "⚠️ 接口频率限制，请稍后重试。（每秒最多 5 次）"
        return f"❌ 删除{type_label}失败：{msg} (code {code})"
    except Exception as error:
        return f"❌ 删除文件异常: {str(error)[:300]}"
