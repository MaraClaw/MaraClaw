from uuid import UUID

from app.records.tool import ToolRecord
from app.records.user import UserRecord
from app.schemas.operator_response_tools import ToolCapabilities


def can_mutate_tool(user: UserRecord, tool: ToolRecord) -> bool:
    return user.is_platform_admin or (
        user.role == "org_admin"
        and user.tenant_id is not None
        and tool.tenant_id is not None
        and tool.tenant_id == user.tenant_id
        and tool.source != "builtin"
    )


def can_configure_tool(user: UserRecord, tool: ToolRecord, target_tenant_id: UUID | None) -> bool:
    if tool.source == "builtin":
        return target_tenant_id is not None and (
            user.is_platform_admin or (user.role == "org_admin" and user.tenant_id == target_tenant_id)
        )
    return can_mutate_tool(user, tool)


def tool_capabilities(user: UserRecord, tool: ToolRecord, target_tenant_id: UUID | None) -> ToolCapabilities:
    can_mutate = can_mutate_tool(user, tool)
    return {
        "can_edit_details": can_mutate,
        "can_delete": can_mutate and tool.type != "builtin",
        "can_configure": can_configure_tool(user, tool, target_tenant_id),
    }
