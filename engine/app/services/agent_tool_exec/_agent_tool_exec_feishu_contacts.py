from __future__ import annotations

import uuid
from contextlib import suppress
from pathlib import Path
from types import ModuleType

from app.dao.agent_dao import agent_dao
from app.dao.org_member_dao import org_member_dao
from app.dao.user_dao import user_dao
from app.services import agent_tools
from app.services.agent_tool_exec.registry import ToolArguments

_cached_users: list[dict[str, str]] = []


def _cached_user_count(facade: ModuleType) -> int:
    if hasattr(facade, "_cached_users"):
        return len(agent_tools._cached_users)
    return len(_cached_users)


def _contacts_cache_file(agent_id: uuid.UUID) -> Path:
    return Path("/data/workspaces") / str(agent_id) / "feishu_contacts_cache.json"


async def _feishu_user_search(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    name_value = arguments.get("name", "")
    name = name_value.strip() if isinstance(name_value, str) else ""
    if not name:
        return "❌ Missing required argument 'name'"
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."

    with suppress(Exception):
        agent = await agent_dao.get(agent_id)
        tenant_id = agent.tenant_id if agent else None
        if tenant_id:
            org_rows = await org_member_dao.list_active_filtered(tenant_id=tenant_id, search=name, limit=50)
            org_members = [
                member for member, _pname, _ptype in org_rows if name.casefold() in (member.name or "").casefold()
            ]
            if not org_members:
                org_members = [member for member, _pname, _ptype in org_rows]
            if org_members:
                lines = [f"🔍 从通讯录找到 {len(org_members)} 位匹配「{name}」的用户：\n"]
                for member in org_members:
                    lines.append(f"• **{member.name}**")
                    if member.external_id:
                        lines.append(f"  user_id: `{member.external_id}`")
                    if member.open_id:
                        lines.append(f"  open_id: `{member.open_id}`")
                    if member.email:
                        lines.append(f"  邮箱: {member.email}")
                    if member.department_path:
                        lines.append(f"  部门: {member.department_path}")
                return "\n".join(lines)

    with suppress(Exception):
        agent = await agent_dao.get(agent_id)
        tenant_id = agent.tenant_id if agent else None
        if tenant_id:
            platform_users = await user_dao.list_active_for_tenant(tenant_id, include_identity=True)
            lowered = name.casefold()
            for platform_user in platform_users:
                display = (platform_user.display_name or "").casefold()
                if lowered not in display:
                    continue
                user_id = getattr(platform_user, "feishu_user_id", None)
                if user_id:
                    result_lines = [f"🔍 找到匹配「{name}」的用户：\n", f"• **{platform_user.display_name}**"]
                    result_lines.append(f"  user_id: `{user_id}`")
                    email = getattr(platform_user, "email", None)
                    if email:
                        result_lines.append(f"  邮箱: {email}")
                    return "\n".join(result_lines)

    total = _cached_user_count(agent_tools)
    if total == 0:
        return (
            f"❌ 本地通讯录缓存为空，暂时无法搜索「{name}」。\n\n"
            "通讯录缓存会在同事向机器人发消息时自动建立。\n"
            "如果「覃睿」从未给机器人发过消息，可以请他先给机器人发一条消息，"
            "之后就能直接搜索到他了。\n\n"
            "或者，请直接告诉我「覃睿」的飞书 open_id 或邮箱，我可以立刻操作。"
        )
    return (
        f"❌ 未在本地通讯录（已缓存 {total} 人）中找到「{name}」。\n\n"
        "通讯录缓存来自给机器人发过消息的同事。\n"
        "如果「{name}」从未给机器人发消息，请他先发一条，之后即可自动识别。\n"
        "或者请直接提供其飞书 open_id / 工作邮箱。"
    )


async def _feishu_contacts_refresh(agent_id: uuid.UUID) -> None:
    with suppress(Exception):
        cache_file = _contacts_cache_file(agent_id)
        if cache_file.exists():
            cache_file.unlink()
