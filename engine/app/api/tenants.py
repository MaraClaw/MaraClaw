"""Tenant (Company) management API.

Public endpoints for joining a company.
Platform-admin endpoint to create a tenant with its genesis org admin.
"""

import io
import re
import uuid
from datetime import UTC, datetime
from typing import TypedDict

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel, EmailStr, Field

from app.core.json_types import JsonObject
from app.core.security import get_current_user, require_role
from app.dao.agent_dao import agent_dao
from app.dao.system_setting_dao import system_setting_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services.admin_audit import field_change, write_admin_audit
from app.services.admin_provisioning import (
    AdminGuardError,
    apply_user_assignment,
    assert_join_may_rewrite_membership,
    is_genesis_platform_admin,
)
from app.services.storage import ensure_local_path, get_storage_backend, normalize_storage_key
from app.services.tenant_provisioning import (
    AdminEmailTakenError,
    GenesisOrgAdminExistsError,
    attach_genesis_org_admin,
    create_tenant_with_org_admin,
    delete_tenant_and_release_identities,
)

router = APIRouter(prefix="/tenants", tags=["tenants"])


async def get_client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


class TokenUsageBucket(TypedDict):
    total_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cache_hit_rate: float


# ─── Schemas ────────────────────────────────────────────


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    admin_email: EmailStr
    admin_password: str = Field(min_length=6, max_length=128)
    admin_display_name: str | None = Field(default=None, max_length=200)


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    im_provider: str
    timezone: str = "UTC"
    country_region: str = "001"
    is_active: bool
    sso_enabled: bool = False
    sso_domain: str | None = None
    a2a_async_enabled: bool = True
    default_model_id: uuid.UUID | None = None
    logo_url: str | None = None
    created_at: datetime | None = None
    is_system: bool = False
    is_default_end_user_org: bool = False

    model_config = {"from_attributes": True}


class EmailDomainOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    domain: str
    is_default: bool
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class EmailDomainCreate(BaseModel):
    domain: str = Field(min_length=1, max_length=255)
    is_default: bool = False


class EmailDomainPatch(BaseModel):
    is_default: bool = True


class SuggestedJoinRequest(BaseModel):
    tenant_id: uuid.UUID | None = None


class OrgSummary(BaseModel):
    id: uuid.UUID
    name: str
    slug: str


class EmailLookupResponse(BaseModel):
    match: OrgSummary | None = None
    fallback: OrgSummary | None = None


class JoinOrgResponse(BaseModel):
    tenant: TenantOut
    role: str
    access_token: str | None = None


class TenantCreateResponse(BaseModel):
    tenant: TenantOut
    org_admin_email: str
    must_change_password: bool = True


class GenesisOrgAdminCreate(BaseModel):
    admin_email: EmailStr
    admin_password: str = Field(min_length=6, max_length=128)
    admin_display_name: str | None = Field(default=None, max_length=200)


class TenantUpdate(BaseModel):
    name: str | None = None
    im_provider: str | None = None
    timezone: str | None = None
    country_region: str | None = None
    is_active: bool | None = None
    sso_enabled: bool | None = None
    sso_domain: str | None = None
    a2a_async_enabled: bool | None = None


def _tenant_logo_key(tenant_id: uuid.UUID) -> str:
    return normalize_storage_key(f"_tenant_logos/{tenant_id}.png")


def _tenant_logo_url(tenant_id: uuid.UUID) -> str:
    return f"/api/tenants/{tenant_id}/logo?v={int(datetime.now(UTC).timestamp())}"


def _system_setting_enabled(value: JsonObject | None) -> bool:
    if value is None:
        return True
    enabled = value.get("enabled")
    return enabled if isinstance(enabled, bool) else True


async def _get_updateable_tenant(
    tenant_id: uuid.UUID,
    current_user: UserRecord,
) -> TenantRecord:
    if current_user.role == "org_admin":
        if not current_user.tenant_id:
            raise HTTPException(status_code=403, detail="Organization admin must belong to a company")
        if current_user.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="Can only update your own company")
    elif current_user.role != "platform_admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    tenant = await tenant_dao.get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


# ─── Platform admin: create tenant with genesis org admin ─


@router.post("/", response_model=TenantCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    data: TenantCreate,
    current_user: UserRecord = Depends(require_role("platform_admin")),
    client_ip: str | None = Depends(get_client_ip),
):
    """Create a company and its genesis org admin (platform admin only).

    The org admin is provisioned with the given email + initial password and
    must change the password after the first successful login.
    """
    identity_is_platform_admin = bool(getattr(getattr(current_user, "identity", None), "is_platform_admin", False))
    if current_user.role != "platform_admin" and not identity_is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin access required")

    try:
        provisioned = await create_tenant_with_org_admin(
            name=data.name,
            admin_email=str(data.admin_email),
            admin_password=data.admin_password,
            admin_display_name=data.admin_display_name,
        )
    except AdminEmailTakenError as exc:
        raise HTTPException(status_code=409, detail="Admin email is already registered") from exc

    await write_admin_audit(
        actor=current_user,
        action="tenant_create",
        target_type="tenant",
        target_id=provisioned.tenant.id,
        tenant_id=provisioned.tenant.id,
        changes={
            "name": field_change(None, provisioned.tenant.name),
            "org_admin_email": field_change(None, provisioned.admin_email),
        },
        details={"org_admin_user_id": str(provisioned.org_admin.id)},
        ip_address=client_ip,
    )
    return TenantCreateResponse(
        tenant=TenantOut.model_validate(provisioned.tenant),
        org_admin_email=provisioned.admin_email,
        must_change_password=True,
    )


# ─── Self-Service: Join Company via Invite Code ─────────


class JoinRequest(BaseModel):
    invitation_code: str = Field(min_length=1, max_length=32)
    target_tenant_id: uuid.UUID | None = None


class JoinResponse(BaseModel):
    tenant: TenantOut
    role: str
    access_token: str | None = None


class TransferRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128)
    invitation_code: str | None = Field(default=None, max_length=32)
    tenant_id: uuid.UUID | None = None


def _require_join_allowed(current_user: UserRecord) -> None:
    try:
        assert_join_may_rewrite_membership(current_user)
    except AdminGuardError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/join", response_model=JoinResponse)
async def join_company(data: JoinRequest, current_user: UserRecord = Depends(get_current_user)):
    """Join an existing company using an invitation code.

    End users may belong to only one organization. A second membership is refused.
    """
    from app.services.org_membership import (
        AlreadyInOrgError,
        InvitationError,
        attach_user_to_org,
        consume_invitation_code,
        require_active_invitation,
    )

    if current_user.tenant_id is None:
        _require_join_allowed(current_user)

    try:
        code_obj = await require_active_invitation(data.invitation_code)
    except InvitationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Verify matching tenant if locked (Dedicated Link flow)
    if data.target_tenant_id and str(code_obj.tenant_id) != str(data.target_tenant_id):
        raise HTTPException(
            status_code=403, detail="This invitation code does not belong to the required organization."
        )

    tenant = await tenant_dao.get(code_obj.tenant_id)
    if not tenant or not tenant.is_active:
        raise HTTPException(status_code=400, detail="Company not found or is disabled")

    if current_user.tenant_id is not None and current_user.tenant_id != tenant.id:
        raise HTTPException(status_code=409, detail="You already belong to an organization")

    try:
        attached = await attach_user_to_org(current_user, tenant)
    except AlreadyInOrgError as exc:
        raise HTTPException(status_code=409, detail=str(exc) or "You already belong to an organization") from exc

    await consume_invitation_code(data.invitation_code)

    return JoinResponse(
        tenant=TenantOut.model_validate(tenant),
        role=attached.role,
        access_token=None,
    )


# ─── Registration Config ───────────────────────────────


@router.get("/registration-config")
async def get_registration_config():
    """Public - tenant creation is platform-admin only; self-create is gone."""
    return {"allow_self_create_company": False, "tenant_creation": "platform_admin_only"}


def _org_summary(tenant) -> OrgSummary:
    return OrgSummary(id=tenant.id, name=tenant.name, slug=tenant.slug)


@router.get("/lookup-by-email", response_model=EmailLookupResponse)
async def lookup_org_by_email(email: str):
    """Public: which org an email would join, plus the OpenClaw fallback."""
    from app.services.org_membership import DefaultOrgUnavailableError, get_fallback_org, lookup_tenant_by_email_domain

    match = await lookup_tenant_by_email_domain(email)
    try:
        fallback = await get_fallback_org()
    except DefaultOrgUnavailableError:
        fallback = None
    return EmailLookupResponse(
        match=_org_summary(match) if match else None,
        fallback=_org_summary(fallback) if fallback else None,
    )


@router.post("/join-suggested", response_model=JoinOrgResponse)
async def join_suggested_org(
    data: SuggestedJoinRequest,
    current_user: UserRecord = Depends(get_current_user),
):
    """Confirm joining the organization suggested by the verified email domain."""
    from app.core.security import create_access_token
    from app.services.org_membership import (
        AlreadyInOrgError,
        attach_user_to_org,
        lookup_tenant_for_verified_email,
    )

    if current_user.tenant_id is not None:
        raise HTTPException(status_code=409, detail="You already belong to an organization")
    _require_join_allowed(current_user)
    suggested = await lookup_tenant_for_verified_email(current_user)
    if suggested is None:
        raise HTTPException(status_code=400, detail="No organization is suggested for this account")
    if data.tenant_id is not None and data.tenant_id != suggested.id:
        raise HTTPException(status_code=400, detail="That organization is not the suggested match")
    try:
        attached = await attach_user_to_org(current_user, suggested)
    except AlreadyInOrgError as exc:
        raise HTTPException(status_code=409, detail=str(exc) or "You already belong to an organization") from exc
    return JoinOrgResponse(
        tenant=TenantOut.model_validate(suggested),
        role=attached.role,
        access_token=create_access_token(str(attached.id), attached.role),
    )


@router.post("/join-default", response_model=JoinOrgResponse)
async def join_default_org(current_user: UserRecord = Depends(get_current_user)):
    """Decline a suggested org and join OpenClaw."""
    from app.core.security import create_access_token
    from app.services.org_membership import (
        AlreadyInOrgError,
        DefaultOrgUnavailableError,
        attach_user_to_org,
        get_fallback_org,
    )

    if current_user.tenant_id is not None:
        raise HTTPException(status_code=409, detail="You already belong to an organization")
    _require_join_allowed(current_user)
    try:
        fallback = await get_fallback_org()
        attached = await attach_user_to_org(current_user, fallback)
    except DefaultOrgUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AlreadyInOrgError as exc:
        raise HTTPException(status_code=409, detail=str(exc) or "You already belong to an organization") from exc
    return JoinOrgResponse(
        tenant=TenantOut.model_validate(fallback),
        role=attached.role,
        access_token=create_access_token(str(attached.id), attached.role),
    )


@router.post("/transfer", response_model=JoinOrgResponse)
async def transfer_organization(
    data: TransferRequest,
    current_user: UserRecord = Depends(get_current_user),
):
    """Move a member from their current org to another after password confirmation."""
    from app.core.security import create_access_token, verify_password_async
    from app.dao.identity_dao import identity_dao
    from app.services.org_membership import (
        AlreadyInOrgError,
        DefaultOrgUnavailableError,
        InvitationError,
        consume_invitation_code,
        get_fallback_org,
        lookup_tenant_for_verified_email,
        require_active_invitation,
        transfer_user_to_org,
    )

    if current_user.tenant_id is None:
        raise HTTPException(status_code=400, detail="Join an organization first, then transfer")
    _require_join_allowed(current_user)

    identity = getattr(current_user, "identity", None) or await identity_dao.get(current_user.identity_id)
    if identity is None or not identity.password_hash:
        raise HTTPException(status_code=400, detail="This account cannot confirm a password")
    if not await verify_password_async(data.password, identity.password_hash):
        raise HTTPException(status_code=401, detail="Password is incorrect")

    target = None
    if data.invitation_code:
        try:
            code_obj = await require_active_invitation(data.invitation_code)
        except InvitationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if data.tenant_id and str(code_obj.tenant_id) != str(data.tenant_id):
            raise HTTPException(status_code=403, detail="This invitation code does not belong to the required organization.")
        target = await tenant_dao.get(code_obj.tenant_id)
        if not target or not target.is_active:
            raise HTTPException(status_code=400, detail="Company not found or is disabled")
    elif data.tenant_id:
        target = await tenant_dao.get(data.tenant_id)
        if not target or not target.is_active:
            raise HTTPException(status_code=400, detail="Company not found or is disabled")
        allowed = await lookup_tenant_for_verified_email(current_user)
        try:
            fallback = await get_fallback_org()
        except DefaultOrgUnavailableError:
            fallback = None
        allowed_ids = {item.id for item in (allowed, fallback) if item is not None}
        if target.id not in allowed_ids:
            raise HTTPException(
                status_code=403,
                detail="Transfer without an invite is only allowed to your email-domain org or OpenClaw",
            )
    else:
        raise HTTPException(status_code=400, detail="Provide an invitation code or a destination organization")

    if target.id == current_user.tenant_id:
        raise HTTPException(status_code=400, detail="You already belong to that organization")

    try:
        attached = await transfer_user_to_org(current_user, target)
    except AlreadyInOrgError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DefaultOrgUnavailableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if data.invitation_code:
        await consume_invitation_code(data.invitation_code)

    return JoinOrgResponse(
        tenant=TenantOut.model_validate(target),
        role=attached.role,
        access_token=create_access_token(str(attached.id), attached.role),
    )


# ─── Public: Resolve Tenant by Domain ───────────────────


@router.get("/resolve-by-domain")
async def resolve_tenant_by_domain(domain: str):
    """Resolve a tenant by its sso_domain or subdomain slug.

    sso_domain is stored as a full URL (e.g. "https://acme.maraclaw.ai" or "http://1.2.3.4:3009").
    The incoming `domain` parameter is the host (without protocol).

    Lookup precedence:
    1. Exact match on tenant.sso_domain ending with the host (strips protocol)
    2. Extract slug from "{slug}.maraclaw.ai" and match tenant.slug
    """
    tenant = None

    sso_redirect_enabled = await system_setting_dao.is_flag_enabled("sso_custom_domain_redirect_enabled", default=True)

    if sso_redirect_enabled:
        # 1. Match by stripping protocol from stored sso_domain
        # sso_domain = "https://acme.maraclaw.ai" → compare against "acme.maraclaw.ai"
        for proto in ("https://", "http://"):
            tenant = await tenant_dao.get_by_sso_domain_exact(f"{proto}{domain}")
            if tenant:
                break

        # 2. Try without port (e.g. domain = "1.2.3.4:3009" → try "1.2.3.4")
        if not tenant and ":" in domain:
            domain_no_port = domain.split(":")[0]
            for proto in ("https://", "http://"):
                tenant = await tenant_dao.get_by_sso_domain_like(f"{proto}{domain_no_port}")
                if tenant:
                    break

    # 3. Fallback: extract slug from subdomain pattern
    if not tenant:
        m = re.match(r"^([a-z0-9][a-z0-9\-]*[a-z0-9])\.maraclaw\.ai$", domain.lower())
        if m:
            slug = m.group(1)
            tenant = await tenant_dao.get_by_slug(slug)

    if not tenant or not tenant.is_active or not tenant.sso_enabled:
        raise HTTPException(status_code=404, detail="Tenant not found or not active or SSO not enabled")

    return {
        "id": tenant.id,
        "name": tenant.name,
        "slug": tenant.slug,
        "sso_enabled": tenant.sso_enabled,
        "sso_domain": tenant.sso_domain,
        "is_active": tenant.is_active,
    }


# ─── Authenticated: List / Get ──────────────────────────


@router.get("/", response_model=list[TenantOut])
async def list_tenants(current_user: UserRecord = Depends(require_role("platform_admin"))):
    """List all tenants (platform_admin only)."""
    tenants = await tenant_dao.list_ordered_by_created_at(desc=True)
    return [TenantOut.model_validate(t) for t in tenants]


@router.get("/me", response_model=TenantOut)
async def get_my_tenant(current_user: UserRecord = Depends(get_current_user)):
    """Return the current user's own tenant. Any authenticated member can read
    this - the wizard and the chat model switcher need default_model_id, which
    shouldn't require admin privileges.
    """
    if not current_user.tenant_id:
        raise HTTPException(status_code=404, detail="User is not in a tenant")
    tenant = await tenant_dao.get(current_user.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return TenantOut.model_validate(tenant)


@router.get("/me/token-usage")
async def get_my_tenant_token_usage(current_user: UserRecord = Depends(get_current_user)):
    """Return aggregate token and prompt-cache usage for the current company."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=404, detail="User is not in a tenant")

    row = await agent_dao.token_usage_for_tenant(current_user.tenant_id)

    def bucket(total: int, cache_read: int, cache_creation: int) -> TokenUsageBucket:
        total = int(total or 0)
        cache_read = int(cache_read or 0)
        return {
            "total_tokens": total,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": int(cache_creation or 0),
            "cache_hit_rate": round(cache_read / total, 4) if total > 0 else 0.0,
        }

    return {
        "today": bucket(row["tokens_today"], row["cache_today"], row["cache_creation_today"]),
        "month": bucket(row["tokens_month"], row["cache_month"], row["cache_creation_month"]),
        "total": bucket(row["tokens_total"], row["cache_total"], row["cache_creation_total"]),
    }


@router.get("/{tenant_id}", response_model=TenantOut)
async def get_tenant(tenant_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Get tenant details. Platform admins can view any; org_admins only their own."""
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    if current_user.role == "org_admin":
        if not current_user.tenant_id:
            raise HTTPException(status_code=403, detail="Organization admin must belong to a company")
        if current_user.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")
    tenant = await tenant_dao.get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return TenantOut.model_validate(tenant)


@router.get("/{tenant_id}/email-domains", response_model=list[EmailDomainOut])
async def list_email_domains(
    tenant_id: uuid.UUID,
    current_user: UserRecord = Depends(require_role("org_admin", "platform_admin")),
):
    from app.dao.tenant_email_domain_dao import tenant_email_domain_dao

    await _get_updateable_tenant(tenant_id, current_user)
    rows = await tenant_email_domain_dao.list_for_tenant(tenant_id)
    return [EmailDomainOut.model_validate(row) for row in rows]


@router.post("/{tenant_id}/email-domains", response_model=EmailDomainOut, status_code=status.HTTP_201_CREATED)
async def create_email_domain(
    tenant_id: uuid.UUID,
    data: EmailDomainCreate,
    current_user: UserRecord = Depends(require_role("org_admin", "platform_admin")),
    client_ip: str | None = Depends(get_client_ip),
):
    from app.services.org_membership import DomainClaimedError, InvalidEmailDomainError, add_email_domain

    await _get_updateable_tenant(tenant_id, current_user)
    try:
        row = await add_email_domain(tenant_id, data.domain, is_default=data.is_default)
    except InvalidEmailDomainError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Invalid email domain") from exc
    except DomainClaimedError as exc:
        raise HTTPException(status_code=409, detail="Email domain is already claimed") from exc
    await write_admin_audit(
        actor=current_user,
        action="tenant_email_domain_add",
        target_type="tenant",
        target_id=tenant_id,
        tenant_id=tenant_id,
        changes={"domain": field_change(None, row.domain)},
        ip_address=client_ip,
    )
    return EmailDomainOut.model_validate(row)


@router.patch("/{tenant_id}/email-domains/{domain_id}", response_model=EmailDomainOut)
async def patch_email_domain(
    tenant_id: uuid.UUID,
    domain_id: uuid.UUID,
    data: EmailDomainPatch,
    current_user: UserRecord = Depends(require_role("org_admin", "platform_admin")),
    client_ip: str | None = Depends(get_client_ip),
):
    from app.services.org_membership import set_default_email_domain

    await _get_updateable_tenant(tenant_id, current_user)
    if not data.is_default:
        raise HTTPException(status_code=400, detail="Clearing the default requires choosing another domain")
    try:
        row = await set_default_email_domain(tenant_id, domain_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Email domain not found") from exc
    await write_admin_audit(
        actor=current_user,
        action="tenant_email_domain_default",
        target_type="tenant",
        target_id=tenant_id,
        tenant_id=tenant_id,
        changes={"default_domain": field_change(None, row.domain)},
        ip_address=client_ip,
    )
    return EmailDomainOut.model_validate(row)


@router.delete("/{tenant_id}/email-domains/{domain_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_email_domain(
    tenant_id: uuid.UUID,
    domain_id: uuid.UUID,
    current_user: UserRecord = Depends(require_role("org_admin", "platform_admin")),
    client_ip: str | None = Depends(get_client_ip),
):
    from app.services.org_membership import delete_email_domain

    await _get_updateable_tenant(tenant_id, current_user)
    try:
        await delete_email_domain(tenant_id, domain_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Email domain not found") from exc
    await write_admin_audit(
        actor=current_user,
        action="tenant_email_domain_delete",
        target_type="tenant",
        target_id=tenant_id,
        tenant_id=tenant_id,
        details={"domain_id": str(domain_id)},
        ip_address=client_ip,
    )
    return


@router.put("/{tenant_id}", response_model=TenantOut)
async def update_tenant(
    tenant_id: uuid.UUID,
    data: TenantUpdate,
    current_user: UserRecord = Depends(require_role("org_admin", "platform_admin")),
    client_ip: str | None = Depends(get_client_ip),
):
    """Update tenant settings. Platform admins can update any; org_admins only their own."""
    if current_user.role == "org_admin":
        if not current_user.tenant_id:
            raise HTTPException(status_code=403, detail="Organization admin must belong to a company")
        if current_user.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="Can only update your own company")
    tenant = await tenant_dao.get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    update_data = data.model_dump(exclude_unset=True)

    # SSO configuration is managed exclusively by the company's own org_admin
    # via the Enterprise Settings page. Platform admins should not override it here.
    if current_user.role == "platform_admin":
        update_data.pop("sso_enabled", None)
        update_data.pop("sso_domain", None)

    if update_data.get("is_active") is False:
        from app.services.org_membership import DefaultOrgUnavailableError, assert_may_deactivate_tenant

        try:
            assert_may_deactivate_tenant(tenant, making_active=False)
        except DefaultOrgUnavailableError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    updated = await tenant_dao.update(db_obj=tenant, obj_in=update_data)
    if update_data:
        await write_admin_audit(
            actor=current_user,
            action="tenant_update",
            target_type="tenant",
            target_id=tenant_id,
            tenant_id=tenant_id,
            changes={key: field_change(getattr(tenant, key, None), value) for key, value in update_data.items()},
            ip_address=client_ip,
        )
    return TenantOut.model_validate(updated)


@router.get("/{tenant_id}/logo")
async def get_tenant_logo(tenant_id: uuid.UUID):
    """Serve a tenant logo. Logos are public UI assets, addressed by UUID."""
    storage = get_storage_backend()
    key = _tenant_logo_key(tenant_id)
    if not await storage.exists(key):
        raise HTTPException(status_code=404, detail="Logo not found")
    path = await ensure_local_path(key)
    return FileResponse(path, media_type="image/png")


@router.post("/{tenant_id}/logo", response_model=TenantOut)
async def upload_tenant_logo(
    tenant_id: uuid.UUID,
    file: UploadFile = File(...),
    current_user: UserRecord = Depends(require_role("org_admin", "platform_admin")),
):
    """Upload a cropped square company logo.

    The frontend crops to a 1:1 PNG before upload. The backend keeps a hard
    1 MB limit and stores the image outside git-managed source files.
    """
    tenant = await _get_updateable_tenant(tenant_id, current_user)
    if file.content_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise HTTPException(status_code=400, detail="Logo must be a PNG, JPEG, or WebP image")

    data = await file.read()
    if len(data) > 1024 * 1024:
        raise HTTPException(status_code=400, detail="Logo image must be 1 MB or smaller")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid image file") from exc
    if image.width != image.height:
        raise HTTPException(status_code=400, detail="Logo image must be a 1:1 square")

    output = io.BytesIO()
    image.convert("RGBA").save(output, format="PNG", optimize=True)
    png_data = output.getvalue()
    if len(png_data) > 1024 * 1024:
        raise HTTPException(status_code=400, detail="Logo image must be 1 MB or smaller after processing")

    storage = get_storage_backend()
    await storage.write_bytes(_tenant_logo_key(tenant_id), png_data, content_type="image/png")

    config = dict(tenant.im_config or {})
    config["logo_url"] = _tenant_logo_url(tenant_id)
    updated = await tenant_dao.update(db_obj=tenant, obj_in={"im_config": config})
    return TenantOut.model_validate(updated)


@router.delete("/{tenant_id}/logo", response_model=TenantOut)
async def delete_tenant_logo(
    tenant_id: uuid.UUID, current_user: UserRecord = Depends(require_role("org_admin", "platform_admin"))
):
    """Remove a custom company logo and fall back to the generated default."""
    tenant = await _get_updateable_tenant(tenant_id, current_user)

    storage = get_storage_backend()
    key = _tenant_logo_key(tenant_id)
    if await storage.exists(key):
        await storage.delete(key)

    config = dict(tenant.im_config or {})
    config.pop("logo_url", None)
    updated = await tenant_dao.update(db_obj=tenant, obj_in={"im_config": config})
    return TenantOut.model_validate(updated)


@router.put("/{tenant_id}/assign-user/{user_id}")
async def assign_user_to_tenant(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str = "member",
    current_user: UserRecord = Depends(require_role("platform_admin")),
    client_ip: str | None = Depends(get_client_ip),
):
    """Assign a user to a tenant with a specific role."""
    if role not in ("agent_admin", "member"):
        raise HTTPException(status_code=400, detail="Invalid role")

    if not await tenant_dao.get(tenant_id):
        raise HTTPException(status_code=404, detail="Tenant not found")

    user = await user_dao.get_with_identity(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        updated = await apply_user_assignment(
            actor=current_user,
            target=user,
            tenant_id=tenant_id,
            role=role,
            ip_address=client_ip,
        )
    except AdminGuardError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"status": "ok", "user_id": str(user_id), "tenant_id": str(tenant_id), "role": updated.role}


@router.post(
    "/{tenant_id}/genesis-org-admin",
    response_model=TenantCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def repair_genesis_org_admin(
    tenant_id: uuid.UUID,
    data: GenesisOrgAdminCreate,
    current_user: UserRecord = Depends(require_role("platform_admin")),
    client_ip: str | None = Depends(get_client_ip),
):
    """Attach a genesis org admin to a tenant that does not have one."""
    if not await is_genesis_platform_admin(current_user):
        raise HTTPException(status_code=403, detail="Only the genesis platform admin can repair a company admin")
    if not await tenant_dao.get(tenant_id):
        raise HTTPException(status_code=404, detail="Tenant not found")
    try:
        provisioned = await attach_genesis_org_admin(
            tenant_id=tenant_id,
            admin_email=str(data.admin_email),
            admin_password=data.admin_password,
            admin_display_name=data.admin_display_name,
        )
    except GenesisOrgAdminExistsError as exc:
        raise HTTPException(status_code=409, detail="This company already has a genesis organization admin") from exc
    except AdminEmailTakenError as exc:
        raise HTTPException(status_code=409, detail="Admin email is already registered") from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Tenant not found") from exc

    await write_admin_audit(
        actor=current_user,
        action="org_admin_repair",
        target_type="user",
        target_id=provisioned.org_admin.id,
        tenant_id=tenant_id,
        changes={
            "role": field_change(None, "org_admin"),
            "admin_email": field_change(None, provisioned.admin_email),
            "is_genesis": field_change(None, True),
        },
        ip_address=client_ip,
    )
    return TenantCreateResponse(
        tenant=TenantOut.model_validate(provisioned.tenant),
        org_admin_email=provisioned.admin_email,
        must_change_password=True,
    )


# ─── Authenticated: Delete Company ─────────────────────


@router.delete("/{tenant_id}")
async def delete_tenant(tenant_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Permanently delete a company and ALL its data.

    Only the org_admin of the specified tenant (or a platform_admin) may call
    this endpoint.  After deletion the caller receives a `fallback_tenant_id`
    pointing to another company the user's identity belongs to, or `None` if
    the user has no other company.

    Deletion is performed in proper FK order to avoid constraint violations:
    agent-level data → agents → OKR/org data → users → tenant.
    """
    # ── Auth check ──────────────────────────────────────────────────────────
    is_platform_admin = getattr(current_user, "role", None) == "platform_admin"
    is_own_org_admin = getattr(current_user, "role", None) == "org_admin" and str(current_user.tenant_id) == str(
        tenant_id
    )
    if not is_platform_admin and not is_own_org_admin:
        raise HTTPException(
            status_code=403, detail="Only the org admin of this company (or a platform admin) can delete it"
        )

    tenant = await tenant_dao.get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    from app.services.org_membership import DefaultOrgUnavailableError, assert_may_delete_tenant

    try:
        assert_may_delete_tenant(tenant)
    except DefaultOrgUnavailableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    identity_id = current_user.identity_id

    await delete_tenant_and_release_identities(tenant_id)
    await write_admin_audit(
        actor=current_user,
        action="tenant_delete",
        target_type="tenant",
        target_id=tenant_id,
        tenant_id=tenant_id,
        changes={"deleted": field_change(False, True)},
        details={"tenant_name": tenant.name},
    )

    fallback = await user_dao.fallback_tenant_for_identity(identity_id, exclude_tenant_id=tenant_id)
    fallback_tenant_id = str(fallback) if fallback else None

    return {"status": "deleted", "fallback_tenant_id": fallback_tenant_id}
