import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.security import get_current_user
from app.dao.agent_dao import agent_dao
from app.dao.user_dao import user_dao
from app.records.user import UserRecord

router = APIRouter(prefix="/users", tags=["users"])


class UserQuotaUpdate(BaseModel):
    quota_message_limit: int | None = None
    quota_message_period: str | None = None
    quota_max_agents: int | None = None
    quota_agent_ttl_hours: int | None = None


class UserOut(BaseModel):
    id: uuid.UUID
    # username/email/display_name can be None for SSO-created users whose Identity
    # was created without explicit values (e.g., DingTalk/Feishu OAuth flow).
    # The frontend should handle None gracefully.
    username: str | None = None
    email: str | None = None
    display_name: str | None = None
    role: str
    is_active: bool
    # Quota fields
    quota_message_limit: int
    quota_message_period: str
    quota_messages_used: int
    quota_max_agents: int
    quota_agent_ttl_hours: int
    # Computed
    agents_count: int = 0
    # Source info
    created_at: str | None = None
    source: str = "registered"  # 'registered' | 'feishu' | 'dingtalk' | 'wecom' | etc.

    model_config = {"from_attributes": True}


def _user_out(u, agents_count: int = 0) -> UserOut:
    return UserOut(
        id=u.id,
        username=u.username or u.email or f"{u.registration_source or 'user'}_{str(u.id)[:8]}",
        email=u.email or "",
        display_name=u.display_name or u.username or "",
        role=u.role,
        is_active=u.is_active,
        quota_message_limit=u.quota_message_limit,
        quota_message_period=u.quota_message_period,
        quota_messages_used=u.quota_messages_used,
        quota_max_agents=u.quota_max_agents,
        quota_agent_ttl_hours=u.quota_agent_ttl_hours,
        agents_count=agents_count,
        created_at=u.created_at.isoformat() if u.created_at else None,
        source=(u.registration_source or "registered"),
    )


@router.get("/", response_model=list[UserOut])
async def list_users(tenant_id: str | None = None, current_user: UserRecord = Depends(get_current_user)):
    """List all users in the specified tenant (admin only)."""
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    # Platform admins can view any tenant; org_admins only their own
    tid = tenant_id if tenant_id and current_user.role == "platform_admin" else str(current_user.tenant_id)
    try:
        tenant_uuid = uuid.UUID(tid)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid tenant_id") from exc

    users = await user_dao.list_for_tenant_ordered(tenant_uuid, include_identity=True)

    out = []
    for u in users:
        agents_count = await agent_dao.count_active_for_creator(u.id)
        out.append(_user_out(u, agents_count=agents_count))
    return out


@router.patch("/{user_id}/quota", response_model=UserOut)
async def update_user_quota(
    user_id: uuid.UUID, data: UserQuotaUpdate, current_user: UserRecord = Depends(get_current_user)
):
    """Update a user's quota settings (admin only)."""
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    user = await user_dao.get_with_identity(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Cannot modify users outside your organization")

    updates = data.model_dump(exclude_unset=True)
    if "quota_message_period" in updates and updates["quota_message_period"] not in (
        "permanent",
        "daily",
        "weekly",
        "monthly",
    ):
        raise HTTPException(status_code=400, detail="Invalid period. Use: permanent, daily, weekly, monthly")

    if updates:
        user = await user_dao.update(db_obj=user, obj_in=updates) or user
        user = await user_dao.get_with_identity(user_id) or user

    agents_count = await agent_dao.count_active_for_creator(user.id)
    return _user_out(user, agents_count=agents_count)


# ─── Role Management ───────────────────────────────────


class RoleUpdate(BaseModel):
    role: str


@router.patch("/{user_id}/role")
async def update_user_role(user_id: uuid.UUID, data: RoleUpdate, current_user: UserRecord = Depends(get_current_user)):
    """Change a user's role within the same company.

    Permissions:
    - org_admin: can set roles to org_admin / member within own tenant.
      Cannot assign platform_admin.
    - platform_admin: can set any valid role.

    Safety:
    - If the target is the ONLY remaining org_admin in the company,
      demoting them is blocked to prevent orphaned companies.
    """
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")

    # Validate target role value
    allowed_roles = ("org_admin", "member")
    if current_user.role == "platform_admin":
        allowed_roles = ("platform_admin", "org_admin", "member")
    if data.role not in allowed_roles:
        raise HTTPException(status_code=400, detail=f"Invalid role. Allowed: {', '.join(allowed_roles)}")

    target_user = await user_dao.get_with_identity(user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    # org_admin can only modify users in the same tenant
    if current_user.role == "org_admin" and target_user.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Cannot modify users outside your organization")

    # No-op shortcut
    if target_user.role == data.role:
        return {"status": "ok", "user_id": str(user_id), "role": data.role}

    # Last-admin protection: if demoting an org_admin, check they are not the only one
    if target_user.role in ("org_admin", "platform_admin") and data.role not in ("org_admin", "platform_admin"):
        admin_count = await user_dao.count_admins_for_tenant(target_user.tenant_id)
        if admin_count <= 1:
            raise HTTPException(
                status_code=400, detail="Cannot demote the only administrator. Promote another user first."
            )

    await user_dao.update(db_obj=target_user, obj_in={"role": data.role})
    return {"status": "ok", "user_id": str(user_id), "role": data.role}
