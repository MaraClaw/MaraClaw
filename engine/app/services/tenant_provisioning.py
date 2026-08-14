"""Provision a tenant together with its genesis org admin."""

from __future__ import annotations

import re
import secrets
import unicodedata
from dataclasses import dataclass

from anyascii import anyascii
from pypinyin import lazy_pinyin

from app.core.security import hash_password_async
from app.dao.identity_dao import identity_dao
from app.dao.participant_dao import participant_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.errors import UniqueViolationError
from app.db.session import connection_ctx
from app.records.tenant import TenantRecord
from app.records.user import UserRecord


class AdminEmailTakenError(Exception):
    """The requested org-admin email already belongs to an identity."""


@dataclass(slots=True, frozen=True)
class ProvisionedTenant:
    tenant: TenantRecord
    org_admin: UserRecord
    admin_email: str


def slugify_tenant_name(name: str) -> str:
    """Generate a URL-friendly slug from a company name.

    Uses a layered transliteration strategy so non-Latin company names produce
    meaningful, readable slugs instead of collapsing to the generic 'company'
    placeholder:

      1. pypinyin   - CJK Han characters -> pinyin (for example, a Chinese company name becomes `gongsi`)
      2. anyascii   - remaining non-ASCII scripts to closest ASCII approximation
                      (Korean '안녕' -> 'annyeong', Japanese 'ひらがな' -> 'hiragana',
                       Arabic 'مرحبا' -> 'mrhb', Cyrillic 'Привет' -> 'Privet', ...)
      3. NFKD norm  - accented Latin chars stripped of diacritics (é -> e)

    A short random hex suffix is always appended to guarantee global uniqueness
    even when two tenants choose the same company name.
    """
    # Step 1: Convert CJK characters to pinyin; non-CJK chars pass through unchanged.
    # lazy_pinyin with errors='default' keeps non-CJK chars as-is so they are
    # handled by the subsequent anyascii pass rather than being silently dropped.
    parts = lazy_pinyin(name, errors="default")
    text = "".join(parts)

    # Step 2: Convert remaining non-ASCII characters using anyascii.
    # anyascii is a no-op on ASCII input, so it is safe to apply to the whole
    # string after pypinyin has already processed the CJK portion.
    text = anyascii(text)

    # Step 3: Normalize any remaining accented Latin chars (é -> e, ü -> u, etc.)
    # and drop anything that still cannot be represented in ASCII.
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")

    # Step 4: Lowercase, collapse non-alphanumeric runs to hyphens, trim to 40 chars.
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().strip())
    slug = slug.strip("-")[:40]

    if not slug:
        # Extremely unlikely after anyascii, but keep as a safety net
        # for inputs that are entirely punctuation or whitespace.
        slug = "company"

    # Add a short random hex suffix to ensure global uniqueness.
    return f"{slug}-{secrets.token_hex(3)}"


async def create_tenant_with_org_admin(
    *,
    name: str,
    admin_email: str,
    admin_password: str,
    admin_display_name: str | None = None,
) -> ProvisionedTenant:
    """Create a tenant and its genesis org admin in one transaction.

    The org admin must change the initial password after first login.
    """
    email = admin_email.strip().lower()
    if await identity_dao.get_by_email(email):
        raise AdminEmailTakenError(email)

    slug = slugify_tenant_name(name)
    password_hash = await hash_password_async(admin_password)
    local_part = email.split("@", 1)[0][:100] or "org-admin"
    username = local_part
    if await identity_dao.is_username_taken(username):
        username = f"{local_part}_{secrets.token_hex(3)}"[:100]

    display_name = (admin_display_name or "").strip() or local_part

    try:
        async with connection_ctx():
            tenant = await tenant_dao.create(obj_in={"name": name, "slug": slug, "im_provider": "web_only"})

            identity = await identity_dao.create_identity(
                email=email,
                username=username,
                password_hash=password_hash,
                is_platform_admin=False,
                email_verified=True,
                must_change_password=True,
            )

            org_admin = await user_dao.create(
                obj_in={
                    "identity_id": identity.id,
                    "tenant_id": tenant.id,
                    "display_name": display_name,
                    "role": "org_admin",
                    "registration_source": "platform_admin",
                    "is_active": True,
                    "is_genesis": True,
                    "quota_message_limit": tenant.default_message_limit,
                    "quota_message_period": tenant.default_message_period,
                    "quota_max_agents": tenant.default_max_agents,
                    "quota_agent_ttl_hours": tenant.default_agent_ttl_hours,
                }
            )
            # Identity-backed email/phone properties require the association for org directory bind.
            org_admin.identity = identity
            await participant_dao.create_for_user(
                org_admin.id,
                display_name=org_admin.display_name,
                avatar_url=org_admin.avatar_url,
            )

            from app.services.registration_service import registration_service

            await registration_service.bind_org_member(org_admin)
    except UniqueViolationError as exc:
        raise AdminEmailTakenError(email) from exc

    return ProvisionedTenant(tenant=tenant, org_admin=org_admin, admin_email=email)


class GenesisOrgAdminExistsError(Exception):
    """The tenant already has a persisted genesis org admin."""


async def attach_genesis_org_admin(
    *,
    tenant_id,
    admin_email: str,
    admin_password: str,
    admin_display_name: str | None = None,
) -> ProvisionedTenant:
    """Create a genesis org admin for a tenant that does not have one."""
    tenant = await tenant_dao.get(tenant_id)
    if tenant is None:
        raise ValueError("tenant not found")
    existing = await user_dao.genesis_org_admin_for_tenant(tenant.id)
    if existing is not None:
        raise GenesisOrgAdminExistsError()

    email = admin_email.strip().lower()
    if await identity_dao.get_by_email(email):
        raise AdminEmailTakenError(email)

    password_hash = await hash_password_async(admin_password)
    local_part = email.split("@", 1)[0][:100] or "org-admin"
    username = local_part
    if await identity_dao.is_username_taken(username):
        username = f"{local_part}_{secrets.token_hex(3)}"[:100]
    display_name = (admin_display_name or "").strip() or local_part

    try:
        async with connection_ctx():
            identity = await identity_dao.create_identity(
                email=email,
                username=username,
                password_hash=password_hash,
                is_platform_admin=False,
                email_verified=True,
                must_change_password=True,
            )
            org_admin = await user_dao.create(
                obj_in={
                    "identity_id": identity.id,
                    "tenant_id": tenant.id,
                    "display_name": display_name,
                    "role": "org_admin",
                    "registration_source": "platform_admin",
                    "is_active": True,
                    "is_genesis": True,
                    "quota_message_limit": tenant.default_message_limit,
                    "quota_message_period": tenant.default_message_period,
                    "quota_max_agents": tenant.default_max_agents,
                    "quota_agent_ttl_hours": tenant.default_agent_ttl_hours,
                }
            )
            org_admin.identity = identity
            await participant_dao.create_for_user(
                org_admin.id,
                display_name=org_admin.display_name,
                avatar_url=org_admin.avatar_url,
            )
            from app.services.registration_service import registration_service

            await registration_service.bind_org_member(org_admin)
    except UniqueViolationError as exc:
        raise AdminEmailTakenError(email) from exc

    return ProvisionedTenant(tenant=tenant, org_admin=org_admin, admin_email=email)


async def delete_tenant_and_release_identities(tenant_id) -> None:
    """Cascade-delete a company and tombstone identities that have no remaining membership."""
    async with connection_ctx():
        identity_ids = await user_dao.list_identity_ids_for_tenant(tenant_id)
        await tenant_dao.delete_cascade(tenant_id)
        await identity_dao.tombstone_orphans(identity_ids)
