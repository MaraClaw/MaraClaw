"""Resolve and attach a user to exactly one organization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from app.core.logging import logger
from app.dao.invitation_code_dao import invitation_code_dao
from app.dao.org_member_dao import org_member_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.tenant_email_domain_dao import tenant_email_domain_dao
from app.dao.user_dao import user_dao
from app.db.errors import UniqueViolationError
from app.db.session import connection_ctx
from app.records.invitation import InvitationCodeRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services.system_org_seeder import OPENCLAW_SLUG

OrgMatchSource = Literal["invite", "domain", "default"]

_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


class InvalidEmailDomainError(ValueError):
    """Raised when a claimed domain is empty or malformed."""


class DomainClaimedError(RuntimeError):
    """Raised when another organization already owns the domain."""


class AlreadyInOrgError(RuntimeError):
    """Raised when an end user already belongs to an organization."""


class DefaultOrgUnavailableError(RuntimeError):
    """Raised when OpenClaw / the fallback org cannot be used."""


class SuggestedOrgMismatchError(RuntimeError):
    """Raised when join-suggested is called for the wrong tenant."""


class InvitationError(ValueError):
    """Invalid, exhausted, or inactive invitation."""


@dataclass(frozen=True, slots=True)
class RegistrationOrgResolution:
    """How a new end user should be placed."""

    matched: TenantRecord | None
    fallback: TenantRecord
    source: OrgMatchSource


def normalize_email_domain(raw: str) -> str:
    """Lowercase a host-like email domain. Reject schemes, wildcards, and bare suffixes."""
    value = (raw or "").strip().lower()
    if value.startswith(("@", "*")):
        raise InvalidEmailDomainError("Invalid email domain")
    value = value.removeprefix("https://").removeprefix("http://")
    value = value.split("/", 1)[0].split(":", 1)[0]
    if not value or " " in value or "@" in value or "://" in value:
        raise InvalidEmailDomainError("Invalid email domain")
    if "." not in value or not _DOMAIN_RE.fullmatch(value):
        raise InvalidEmailDomainError("Invalid email domain")
    return value


def email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    try:
        return normalize_email_domain(email.rsplit("@", 1)[1])
    except InvalidEmailDomainError:
        return None


async def get_fallback_org() -> TenantRecord:
    tenant = await tenant_dao.get_default_end_user_org()
    if tenant is None:
        tenant = await tenant_dao.get_by_slug(OPENCLAW_SLUG)
    if tenant is None or not tenant.is_active:
        raise DefaultOrgUnavailableError("Default organization is not available")
    return tenant


async def lookup_tenant_by_email_domain(email: str | None) -> TenantRecord | None:
    domain = email_domain(email)
    if domain is None:
        return None
    claim = await tenant_email_domain_dao.get_by_domain(domain)
    if claim is None:
        return None
    tenant = await tenant_dao.get(claim.tenant_id)
    if tenant is None or not tenant.is_active:
        return None
    return tenant


async def lookup_tenant_for_verified_email(user: UserRecord) -> TenantRecord | None:
    """Domain match only when the account email has been verified."""
    if not getattr(user, "email_verified", False):
        return None
    return await lookup_tenant_by_email_domain(getattr(user, "email", None))


async def require_active_invitation(code: str) -> InvitationCodeRecord:
    inv = await invitation_code_dao.get_active_by_code(code)
    if inv is None or inv.used_count >= inv.max_uses:
        raise InvitationError("Invalid invitation code")
    return inv


async def consume_invitation_code(code: str | None) -> None:
    """Increment used_count after a successful attach. Missing codes are a no-op."""
    if not code:
        return
    inv = await invitation_code_dao.get_active_by_code(code)
    if inv is None:
        return
    await invitation_code_dao.update(db_obj=inv, obj_in={"used_count": inv.used_count + 1})


async def resolve_registration_org(
    *,
    email: str | None = None,
    invitation_code: str | None = None,
) -> RegistrationOrgResolution:
    """Invite wins, then claimed domain, else the default end-user org."""
    fallback = await get_fallback_org()

    if invitation_code:
        inv = await require_active_invitation(invitation_code)
        invited = await tenant_dao.get(inv.tenant_id)
        if invited is None or not invited.is_active:
            raise InvitationError("Invitation code tenant is inactive")
        return RegistrationOrgResolution(matched=invited, fallback=fallback, source="invite")

    matched = await lookup_tenant_by_email_domain(email)
    if matched is not None:
        return RegistrationOrgResolution(matched=matched, fallback=fallback, source="domain")
    return RegistrationOrgResolution(matched=None, fallback=fallback, source="default")


def suggested_org_payload(tenant: TenantRecord) -> dict[str, object]:
    return {"id": tenant.id, "name": tenant.name, "slug": tenant.slug}


@dataclass(frozen=True, slots=True)
class NewUserPlacement:
    """Where a newly created member row should sit."""

    tenant_id: UUID | None
    suggested: TenantRecord | None
    needs_org_confirm: bool


async def place_new_registration(
    *,
    email: str | None = None,
    invitation_code: str | None = None,
) -> NewUserPlacement:
    """Invite and unmatched emails attach now; claimed domains wait for confirm."""
    resolution = await resolve_registration_org(email=email, invitation_code=invitation_code)
    if resolution.source == "invite" and resolution.matched is not None:
        return NewUserPlacement(tenant_id=resolution.matched.id, suggested=None, needs_org_confirm=False)
    if resolution.source == "domain" and resolution.matched is not None:
        return NewUserPlacement(tenant_id=None, suggested=resolution.matched, needs_org_confirm=True)
    return NewUserPlacement(tenant_id=resolution.fallback.id, suggested=None, needs_org_confirm=False)


def _is_protected_admin(user: UserRecord) -> bool:
    return bool(getattr(user, "is_genesis", False) or getattr(user, "role", None) in {"org_admin", "platform_admin"})


async def attach_user_to_org(user: UserRecord, tenant: TenantRecord, *, role: str = "member") -> UserRecord:
    """Set tenant_id in place. Refuses a second organization."""
    if user.tenant_id == tenant.id:
        return user
    if getattr(user, "is_genesis", False) or getattr(user, "role", None) == "platform_admin":
        raise AlreadyInOrgError("Admin membership cannot be converted this way")
    if user.tenant_id is not None and user.tenant_id != tenant.id:
        raise AlreadyInOrgError("User already belongs to an organization")
    return await _set_user_org(user, tenant, role=role)


async def transfer_user_to_org(user: UserRecord, tenant: TenantRecord) -> UserRecord:
    """Move an existing membership to another organization in place."""
    if user.tenant_id is None:
        return await attach_user_to_org(user, tenant)
    if user.tenant_id == tenant.id:
        return user
    if _is_protected_admin(user):
        raise AlreadyInOrgError("Admin membership cannot be transferred this way")
    return await _set_user_org(user, tenant, role="member")


async def _set_user_org(user: UserRecord, tenant: TenantRecord, *, role: str) -> UserRecord:
    if not tenant.is_active:
        raise DefaultOrgUnavailableError("Organization is disabled")
    previous_tenant_id = user.tenant_id
    try:
        async with connection_ctx():
            if previous_tenant_id is not None and previous_tenant_id != tenant.id:
                await org_member_dao.unbind_user_from_tenant(user.id, previous_tenant_id)
            updated = await user_dao.update(
                db_obj=user,
                obj_in={
                    "tenant_id": tenant.id,
                    "role": role,
                    "is_genesis": False,
                    "quota_message_limit": tenant.default_message_limit,
                    "quota_message_period": tenant.default_message_period,
                    "quota_max_agents": tenant.default_max_agents,
                    "quota_agent_ttl_hours": tenant.default_agent_ttl_hours,
                },
            )
            attached = updated or user
            from app.services.registration_service import registration_service

            await registration_service.bind_org_member(attached)
            return attached
    except UniqueViolationError as exc:
        logger.info("Refusing second organization for identity: %s", exc)
        raise AlreadyInOrgError("User already belongs to an organization") from exc


async def add_email_domain(tenant_id: UUID, raw_domain: str, *, is_default: bool = False):
    domain = normalize_email_domain(raw_domain)
    existing = await tenant_email_domain_dao.get_by_domain(domain)
    if existing is not None:
        if existing.tenant_id != tenant_id:
            raise DomainClaimedError("Email domain is already claimed")
        return existing
    existing_rows = await tenant_email_domain_dao.list_for_tenant(tenant_id)
    make_default = is_default or not existing_rows
    if make_default:
        await tenant_email_domain_dao.clear_default_for_tenant(tenant_id)
    try:
        return await tenant_email_domain_dao.create(
            obj_in={"tenant_id": tenant_id, "domain": domain, "is_default": make_default}
        )
    except UniqueViolationError as exc:
        raise DomainClaimedError("Email domain is already claimed") from exc


async def set_default_email_domain(tenant_id: UUID, domain_id: UUID):
    row = await tenant_email_domain_dao.get(domain_id)
    if row is None or row.tenant_id != tenant_id:
        raise KeyError("email domain not found")
    await tenant_email_domain_dao.clear_default_for_tenant(tenant_id)
    return await tenant_email_domain_dao.update(db_obj=row, obj_in={"is_default": True}) or row


async def delete_email_domain(tenant_id: UUID, domain_id: UUID, *, successor_id: UUID | None = None) -> None:
    row = await tenant_email_domain_dao.get(domain_id)
    if row is None or row.tenant_id != tenant_id:
        raise KeyError("email domain not found")
    remaining = [item for item in await tenant_email_domain_dao.list_for_tenant(tenant_id) if item.id != domain_id]
    if row.is_default and remaining:
        successor = None
        if successor_id is not None:
            successor = next((item for item in remaining if item.id == successor_id), None)
            if successor is None:
                raise KeyError("successor email domain not found")
        else:
            successor = remaining[0]
        await tenant_email_domain_dao.update(db_obj=successor, obj_in={"is_default": True})
    await tenant_email_domain_dao.delete(id=domain_id)


def assert_may_deactivate_tenant(tenant: TenantRecord, *, making_active: bool) -> None:
    if making_active:
        return
    if tenant.is_default_end_user_org:
        raise DefaultOrgUnavailableError("Cannot disable the default end-user organization")


def assert_may_delete_tenant(tenant: TenantRecord) -> None:
    if getattr(tenant, "is_system", False) or getattr(tenant, "is_default_end_user_org", False):
        raise DefaultOrgUnavailableError("System organizations cannot be deleted")
