import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from app.core.security import get_current_user, require_role
from app.dao.admin_audit_dao import admin_audit_log_dao
from app.dao.agent_dao import agent_dao
from app.dao.user_dao import user_dao
from app.records.user import UserRecord
from app.services.admin_audit import field_change, write_admin_audit
from app.services.admin_provisioning import (
    AdminActivationError,
    AdminGuardError,
    apply_user_role_change,
    create_additional_org_admin,
    is_genesis_org_admin,
    set_peer_admin_active,
)
from app.services.tenant_provisioning import AdminEmailTakenError


async def get_client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


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
        before = {key: getattr(user, key, None) for key in updates}
        user = await user_dao.update(db_obj=user, obj_in=updates) or user
        user = await user_dao.get_with_identity(user_id) or user
        await write_admin_audit(
            actor=current_user,
            action="user_quota_update",
            target_type="user",
            target_id=user.id,
            tenant_id=user.tenant_id,
            changes={key: field_change(before.get(key), value) for key, value in updates.items()},
        )

    agents_count = await agent_dao.count_active_for_creator(user.id)
    return _user_out(user, agents_count=agents_count)


# ─── Role Management ───────────────────────────────────


class RoleUpdate(BaseModel):
    role: str


class OrgAdminCreateRequest(BaseModel):
    admin_email: EmailStr
    admin_password: str = Field(min_length=6, max_length=128)
    admin_display_name: str | None = Field(default=None, max_length=200)


class OrgAdminCreateResponse(BaseModel):
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    admin_email: str
    must_change_password: bool = True


class AdminActiveUpdate(BaseModel):
    is_active: bool


class OrgAdminOut(BaseModel):
    id: uuid.UUID
    email: str | None = None
    display_name: str | None = None
    role: str
    is_active: bool
    is_genesis: bool = False
    tenant_id: uuid.UUID | None = None
    created_at: datetime | None = None


class AdminAuditLogOut(BaseModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None = None
    actor_role: str
    actor_email: str | None = None
    action: str
    target_type: str
    target_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    changes: dict
    details: dict
    ip_address: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


@router.post("/org-admins", response_model=OrgAdminCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_org_admin(
    data: OrgAdminCreateRequest,
    current_user: UserRecord = Depends(require_role("org_admin")),
    client_ip: str | None = Depends(get_client_ip),
):
    """Create another org admin in the caller's company. Genesis org admin only."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Organization admin must belong to a company")
    if not await is_genesis_org_admin(current_user):
        raise HTTPException(status_code=403, detail="Only the genesis organization admin can create org admins")

    try:
        provisioned = await create_additional_org_admin(
            tenant_id=current_user.tenant_id,
            admin_email=str(data.admin_email),
            admin_password=data.admin_password,
            admin_display_name=data.admin_display_name,
        )
    except AdminEmailTakenError as exc:
        raise HTTPException(status_code=409, detail="Admin email is already registered") from exc

    tenant_id = provisioned.user.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=500, detail="Org admin was created without a company")
    await write_admin_audit(
        actor=current_user,
        action="org_admin_create",
        target_type="user",
        target_id=provisioned.user.id,
        tenant_id=tenant_id,
        changes={"role": field_change(None, "org_admin"), "admin_email": field_change(None, provisioned.admin_email)},
        details={"must_change_password": True},
        ip_address=client_ip,
    )
    return OrgAdminCreateResponse(
        user_id=provisioned.user.id,
        tenant_id=tenant_id,
        admin_email=provisioned.admin_email,
        must_change_password=True,
    )


@router.get("/org-admins", response_model=list[OrgAdminOut])
async def list_org_admins(current_user: UserRecord = Depends(require_role("org_admin"))):
    """List org admins in the caller's company."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Organization admin must belong to a company")
    users = await user_dao.list_org_admins_for_tenant(current_user.tenant_id)
    return [
        OrgAdminOut(
            id=u.id,
            email=u.email,
            display_name=u.display_name,
            role=u.role,
            is_active=u.is_active,
            is_genesis=bool(getattr(u, "is_genesis", False)),
            tenant_id=u.tenant_id,
            created_at=u.created_at,
        )
        for u in users
    ]


@router.patch("/org-admins/{user_id}/active", response_model=OrgAdminOut)
async def set_org_admin_active(
    user_id: uuid.UUID,
    data: AdminActiveUpdate,
    current_user: UserRecord = Depends(require_role("org_admin")),
    client_ip: str | None = Depends(get_client_ip),
):
    """Activate or deactivate another org admin. Genesis org admin only."""
    target = await user_dao.get_with_identity(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        updated = await set_peer_admin_active(
            actor=current_user,
            target=target,
            is_active=data.is_active,
            ip_address=client_ip,
        )
    except AdminActivationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return OrgAdminOut(
        id=updated.id,
        email=updated.email if updated.identity else target.email,
        display_name=updated.display_name,
        role=updated.role,
        is_active=updated.is_active,
        is_genesis=bool(getattr(updated, "is_genesis", getattr(target, "is_genesis", False))),
        tenant_id=updated.tenant_id,
        created_at=updated.created_at,
    )


@router.get("/admin-audit-logs", response_model=list[AdminAuditLogOut])
async def list_org_admin_audit_logs(
    action: str | None = None,
    limit: int = 100,
    current_user: UserRecord = Depends(require_role("org_admin")),
):
    """List admin action logs for the caller's company."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Organization admin must belong to a company")
    logs = await admin_audit_log_dao.list_recent(
        tenant_id=current_user.tenant_id,
        action=action,
        limit=min(max(limit, 1), 500),
    )
    return [AdminAuditLogOut.model_validate(log) for log in logs]


@router.patch("/{user_id}/role")
async def update_user_role(
    user_id: uuid.UUID,
    data: RoleUpdate,
    current_user: UserRecord = Depends(get_current_user),
    client_ip: str | None = Depends(get_client_ip),
):
    """Change a user's role within the same company.

    Permissions:
    - Genesis org admin: may set ``org_admin`` / ``member`` in own tenant.
    - Other org admins: may set ``member`` only (cannot mint org admins).
    - Genesis platform admin: may set ``platform_admin`` / ``member``.
    - Other platform admins: may set ``member`` only.
    - Nobody may assign the other admin type.

    Safety:
    - Genesis rows cannot change role.
    - The last *active* org / platform admin cannot be demoted.
    """
    if data.role not in ("platform_admin", "org_admin", "member"):
        raise HTTPException(status_code=400, detail="Invalid role. Allowed: platform_admin, org_admin, member")
    target_user = await user_dao.get_with_identity(user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        updated = await apply_user_role_change(
            actor=current_user,
            target=target_user,
            new_role=data.role,
            ip_address=client_ip,
        )
    except AdminGuardError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"status": "ok", "user_id": str(user_id), "role": updated.role}
