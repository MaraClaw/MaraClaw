"""SSO (Single Sign-On) service for enterprise user authentication.

Pure-psycopg implementation via DAOs/records.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, ClassVar

from app.core.logging import logger
from app.dao import identity_dao, identity_provider_dao, org_member_dao, tenant_dao, user_dao
from app.records.org import OrgMemberRecord
from app.records.user import UserRecord
from app.services.platform_service import platform_service


class SSOService:
    """Service for handling SSO authentication flows."""

    DOMAIN_TENANT_HINTS: ClassVar[dict[str, str]] = {}

    async def match_user_by_email(self, email: str, tenant_id: str | None) -> UserRecord | None:
        """Find an existing user by email, including pending (no-org) rows.

        Prefer a same-tenant match, then a pending ``tenant_id is None`` member
        (so SSO can finish org confirm), then any other non-platform-admin row.
        """
        if not email:
            return None
        user = await user_dao.get_by_email_and_tenant(email, tenant_id)
        if user and user.is_active:
            return await user_dao.get_with_identity(user.id)

        identity = await identity_dao.get_by_email(email)
        if not identity:
            return None
        users = await user_dao.get_by_identity_id(identity.id, include_identity=True)
        pending = None
        any_member = None
        for candidate in users:
            if getattr(candidate, "role", None) == "platform_admin":
                continue
            if tenant_id is not None and candidate.tenant_id is not None and str(candidate.tenant_id) == str(tenant_id):
                return candidate
            if candidate.tenant_id is None and pending is None:
                pending = candidate
            elif any_member is None:
                any_member = candidate
        if tenant_id is None:
            return pending or any_member
        return pending or any_member

    async def match_user_by_mobile(self, mobile: str, tenant_id: str) -> UserRecord | None:
        """Find existing active user by mobile phone number."""
        normalized_mobile = re.sub(r"[\s\-\+]", "", mobile)
        if not normalized_mobile:
            return None
        user = await user_dao.get_by_phone_and_tenant(normalized_mobile, tenant_id)
        if user and user.is_active:
            return await user_dao.get_with_identity(user.id)

        identity = await identity_dao.get_by_phone(normalized_mobile)
        if not identity:
            return None
        users = await user_dao.get_by_identity_id(identity.id, include_identity=True)
        for candidate in users:
            if candidate.is_active and str(candidate.tenant_id) == str(tenant_id):
                return candidate
        return None

    async def auto_associate_tenant(self, email: str) -> str | None:
        """Detect tenant based on email domain."""
        if not email or "@" not in email:
            return None
        domain = email.split("@")[1].lower()
        if domain in self.DOMAIN_TENANT_HINTS:
            return self.DOMAIN_TENANT_HINTS[domain]

        tenant = await tenant_dao.find_by_sso_domain_ilike(domain)
        if tenant:
            return str(tenant.id)

        tenant = await tenant_dao.find_by_name_ilike(domain.split(".")[0])
        if tenant:
            return str(tenant.id)
        return None

    async def resolve_user_identity(
        self,
        provider_user_id: str,
        provider_type: str,
        tenant_id: str | None = None,
        identity_data: dict[str, Any] | None = None,
        **_compat: Any,
    ) -> UserRecord | None:
        """Resolve user from external identity via OrgMember."""
        # Accept legacy `db=` kwarg for gradual call-site migration.
        _compat.pop("db", None)

        provider = await identity_provider_dao.get_preferred(provider_type, tenant_id)
        if not provider:
            return None

        member = await self._find_identity_member(
            provider.id,
            provider_type,
            provider_user_id,
            identity_data,
        )
        if not member or not member.user_id:
            return None
        return await user_dao.get_with_identity(member.user_id)

    def _get_identity_payload(self, identity_data: dict[str, Any] | None) -> dict[str, Any]:
        if not identity_data:
            return {}
        raw_data = identity_data.get("raw_data")
        if isinstance(raw_data, dict):
            return raw_data
        return identity_data

    def _extract_identity_ids(
        self,
        provider_type: str,
        provider_user_id: str,
        identity_data: dict[str, Any] | None,
    ) -> tuple[str | None, str | None, str | None]:
        payload = self._get_identity_payload(identity_data)
        identity_data = identity_data or {}

        raw_open_id = (
            payload.get("open_id")
            or payload.get("openId")
            or identity_data.get("open_id")
            or identity_data.get("openId")
        )
        raw_union_id = (
            payload.get("union_id")
            or payload.get("unionId")
            or identity_data.get("union_id")
            or identity_data.get("unionId")
        )

        external_id = None
        if provider_type == "feishu":
            external_id = payload.get("user_id") or (identity_data or {}).get("user_id")
        elif provider_type == "dingtalk":
            external_id = (
                payload.get("userid")
                or payload.get("staffId")
                or (identity_data or {}).get("userid")
                or (identity_data or {}).get("staffId")
            )
        elif provider_type == "wecom":
            external_id = provider_user_id

        open_id = (raw_open_id or "").strip() or None
        union_id = (raw_union_id or "").strip() or None
        external_id = (external_id or "").strip() or None
        return union_id, open_id, external_id

    def _identity_lookup_chain(
        self,
        provider_type: str,
        provider_user_id: str,
        identity_data: dict[str, Any] | None,
    ) -> list[tuple[str, str]]:
        raw_union_id, raw_open_id, raw_external_id = self._extract_identity_ids(
            provider_type, provider_user_id, identity_data
        )
        lookup_chain: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def add(field: str, value: str | None) -> None:
            normalized = (value or "").strip()
            key = (field, normalized)
            if not normalized or key in seen:
                return
            seen.add(key)
            lookup_chain.append(key)

        add("unionid", raw_union_id)
        add("external_id", raw_external_id)
        add("open_id", raw_open_id)
        return lookup_chain

    async def _find_identity_member(
        self,
        provider_id: uuid.UUID,
        provider_type: str,
        provider_user_id: str,
        identity_data: dict[str, Any] | None = None,
    ) -> OrgMemberRecord | None:
        for field, lookup_value in self._identity_lookup_chain(provider_type, provider_user_id, identity_data):
            member = await org_member_dao.find_active_by_provider_field(provider_id, field, lookup_value)
            if member:
                return member
        return None

    async def link_identity(
        self,
        user_id: str,
        provider_type: str,
        provider_user_id: str,
        identity_data: dict[str, Any] | None = None,
        tenant_id: str | None = None,
        **_compat: Any,
    ) -> OrgMemberRecord:
        """Link an external identity to an existing user via OrgMember."""
        _compat.pop("db", None)

        provider = await identity_provider_dao.get_preferred(provider_type, tenant_id)
        if not provider:
            raise ValueError(f"Provider {provider_type} not found for tenant {tenant_id}")

        uid = uuid.UUID(user_id) if isinstance(user_id, str) else user_id
        raw_union_id, raw_open_id, raw_external_id = self._extract_identity_ids(
            provider_type, provider_user_id, identity_data
        )
        member = await self._find_identity_member(
            provider.id,
            provider_type,
            provider_user_id,
            identity_data,
        )

        if member:
            fields: dict[str, Any] = {"user_id": uid}
            if raw_external_id and not member.external_id:
                fields["external_id"] = raw_external_id
            if raw_open_id and not member.open_id:
                fields["open_id"] = raw_open_id
            if (
                raw_union_id
                and member.unionid != raw_union_id
                and (not member.unionid or member.unionid in {provider_user_id, member.open_id, member.external_id})
            ):
                fields["unionid"] = raw_union_id

            if identity_data:
                incoming_name = identity_data.get("name") or identity_data.get("display_name")
                is_placeholder_name = (
                    not member.name
                    or member.name == member.external_id
                    or member.name == provider_user_id
                    or member.name.startswith(f"{provider_type.capitalize()} User")
                )
                if incoming_name and is_placeholder_name:
                    fields["name"] = incoming_name

                incoming_email = identity_data.get("email") or identity_data.get("biz_mail")
                if incoming_email and not member.email:
                    fields["email"] = incoming_email

                incoming_avatar = identity_data.get("avatar")
                if incoming_avatar and not member.avatar_url:
                    fields["avatar_url"] = incoming_avatar

                incoming_mobile = identity_data.get("mobile")
                if incoming_mobile and not member.phone:
                    fields["phone"] = incoming_mobile

            updated = await org_member_dao.update_fields(member.id, fields)
            return updated or member

        member_name = (identity_data.get("name") or identity_data.get("display_name")) if identity_data else None
        return await org_member_dao.create(
            obj_in={
                "name": member_name or f"{provider_type.capitalize()} User {provider_user_id[:8]}",
                "email": (identity_data.get("email") or identity_data.get("biz_mail")) if identity_data else None,
                "avatar_url": identity_data.get("avatar") if identity_data else None,
                "phone": identity_data.get("mobile") if identity_data else None,
                "provider_id": provider.id,
                "user_id": uid,
                "tenant_id": tenant_id,
                "external_id": raw_external_id,
                "unionid": raw_union_id if provider_type != "wecom" else None,
                "open_id": raw_open_id,
                "status": "active",
            }
        )

    async def unlink_identity(
        self,
        user_id: str,
        provider_type: str,
        tenant_id: str | None = None,
        **_compat: Any,
    ) -> bool:
        """Unlink an external identity (OrgMember) from a user."""
        _compat.pop("db", None)

        provider = await identity_provider_dao.get_preferred(provider_type, tenant_id)
        if not provider:
            return False

        mid = uuid.UUID(user_id) if isinstance(user_id, str) else user_id
        members = await org_member_dao.list_by_user_and_provider(mid, provider.id)
        if not members:
            return False
        for member in members:
            await org_member_dao.update_fields(member.id, {"user_id": None})
        return True

    async def check_duplicate_identity(
        self,
        provider_type: str,
        provider_user_id: str,
        tenant_id: str | None = None,
        identity_data: dict[str, Any] | None = None,
        **_compat: Any,
    ) -> UserRecord | None:
        """Check if an external identity is already linked to another user."""
        _compat.pop("db", None)
        return await self.resolve_user_identity(
            provider_user_id,
            provider_type,
            tenant_id,
            identity_data=identity_data,
        )

    async def validate_sso_enablement(self, tenant_id: uuid.UUID, **_compat: Any) -> bool:
        """Check if SSO can be enabled for this tenant under IP restrictions."""
        _compat.pop("db", None)

        tenant = await tenant_dao.get(tenant_id)
        if tenant and tenant.sso_enabled:
            return True

        base_url = await platform_service.get_public_base_url(None)
        parts = base_url.split("://")
        if len(parts) < 2:
            return True

        host = parts[1].split(":")[0].split("/")[0]
        if not platform_service.is_ip_address(host):
            return True

        other_providers = await identity_provider_dao.list_active_sso_excluding_tenant(tenant_id)
        if other_providers:
            conflict_names = []
            for other_provider in other_providers:
                conflict_tenant = await tenant_dao.get(other_provider.tenant_id) if other_provider.tenant_id else None
                name = conflict_tenant.name if conflict_tenant else str(other_provider.tenant_id)
                conflict_names.append(f"'{name}'")
            logger.warning(
                f"[SSO] IP conflict: tenant_id={tenant_id} cannot enable SSO, "
                f"other tenants already have SSO enabled on IP base: {', '.join(conflict_names)}"
            )
        return len(other_providers) == 0

    def add_domain_hint(self, domain: str, tenant_id: str):
        """Add a domain to tenant mapping hint."""
        self.DOMAIN_TENANT_HINTS[domain.lower()] = tenant_id


sso_service = SSOService()
