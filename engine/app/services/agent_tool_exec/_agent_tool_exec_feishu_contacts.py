from __future__ import annotations

import uuid
from contextlib import suppress

from app.dao.agent_dao import agent_dao
from app.dao.org_member_dao import org_member_dao
from app.dao.user_dao import user_dao
from app.services import agent_tools
from app.services.agent_tool_exec.registry import ToolArguments


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
                    if member.external_id not in (None, ""):
                        lines.append(f"  user_id: `{member.external_id}`")
                    if member.open_id not in (None, ""):
                        lines.append(f"  open_id: `{member.open_id}`")
                    if member.email not in (None, ""):
                        lines.append(f"  邮箱: {member.email}")
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

    return f"❌ 未在通讯录中找到「{name}」。请直接提供其飞书 open_id / 工作邮箱。"
