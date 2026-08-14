"""Member persistence helpers for organization sync adapters."""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

from app.dao import identity_dao, org_member_dao, user_dao
from app.dao.org_department_dao import org_department_dao
from app.records.identity import IdentityProviderRecord
from app.records.org import OrgMemberRecord
from app.records.user import UserRecord
from app.services.org_sync.types import ExternalUser
from app.services.org_sync.utils import Style, _anyascii, _normalize_contact, _utcnow, lazy_pinyin, pinyin


class OrgSyncMemberMixin:
    """Shared member persistence behavior for organization sync adapters."""

    provider_type: ClassVar[str]
    tenant_id: uuid.UUID | None

    async def _upsert_member(
        self,
        db: object | None,
        provider: IdentityProviderRecord,
        user: ExternalUser,
        department_external_id: str,
    ) -> dict[str, Any]:
        """Insert or update a member, platform user, and identity."""
        del db
        stats = {"user_created": False, "profile_synced": False}
        self._validate_member_identifiers(provider, user)

        # Find department using user's actual department list.
        # DingTalk's dept_id_list last item is the most specific (leaf) department.
        department = None
        if user.department_ids:
            for dept_ext_id in reversed(user.department_ids):
                department = await org_department_dao.get_by_external(
                    external_id=dept_ext_id,
                    provider_id=provider.id,
                )
                if department:
                    break
        if not department and user.department_external_id:
            department = await org_department_dao.get_by_external(
                external_id=user.department_external_id,
                provider_id=provider.id,
            )

        existing_member = await self._find_existing_member(None, provider, user)

        now = _utcnow()
        user_id = None
        platform_user: UserRecord | None = None
        email = _normalize_contact(user.email)
        mobile = _normalize_contact(user.mobile)

        if email:
            platform_user = await user_dao.get_by_email_and_tenant(email, self.tenant_id)
            if platform_user:
                user_id = platform_user.id

        if not user_id and mobile:
            platform_user = await user_dao.get_by_phone_and_tenant(mobile, self.tenant_id)
            if platform_user:
                user_id = platform_user.id

        translit_full = _anyascii("".join(lazy_pinyin(user.name, errors="default")))
        translit_initial = "".join([i[0] for i in pinyin(user.name, style=Style.FIRST_LETTER)])

        if existing_member:
            updates: dict[str, Any] = {
                "name": user.name,
                "name_translit_full": translit_full,
                "name_translit_initial": translit_initial,
                "avatar_url": user.avatar_url,
                "title": user.title,
                "department_id": department.id if department else None,
                "department_path": department.path if department else user.department_path,
                "status": user.status,
                "external_id": user.external_id,
                "open_id": user.open_id,
                "unionid": user.unionid,
                "provider_id": provider.id,
                "synced_at": now,
            }
            if email is not None:
                updates["email"] = email
            if mobile is not None:
                updates["phone"] = mobile
            if user_id and not existing_member.user_id:
                updates["user_id"] = user_id
            existing_member = await org_member_dao.update(db_obj=existing_member, obj_in=updates)
            stats["profile_synced"] = True
        else:
            existing_member = await org_member_dao.create(
                obj_in={
                    "external_id": user.external_id,
                    "open_id": user.open_id,
                    "unionid": user.unionid,
                    "provider_id": provider.id,
                    "user_id": user_id,
                    "name": user.name,
                    "name_translit_full": translit_full,
                    "name_translit_initial": translit_initial,
                    "email": email,
                    "avatar_url": user.avatar_url,
                    "title": user.title,
                    "department_id": department.id if department else None,
                    "department_path": department.path if department else user.department_path,
                    "phone": mobile,
                    "status": user.status,
                    "tenant_id": self.tenant_id,
                    "synced_at": now,
                }
            )
            stats["profile_synced"] = True

        # Sync email/phone from OrgMember to linked identity
        target_user = platform_user
        if not target_user and (user_id or (existing_member and existing_member.user_id)):
            target_id = user_id or (existing_member.user_id if existing_member else None)
            if target_id:
                target_user = await user_dao.get_with_identity(target_id)

        if target_user and target_user.identity_id:
            identity = await identity_dao.get(target_user.identity_id)
            if identity:
                id_updates: dict[str, Any] = {}
                if email and identity.email != email:
                    id_updates["email"] = email
                if mobile and identity.phone != mobile:
                    id_updates["phone"] = mobile
                if id_updates:
                    _ = await identity_dao.update(db_obj=identity, obj_in=id_updates)

        return stats

    def _provider_requires_unionid(self, provider: IdentityProviderRecord) -> bool:
        provider_type = (provider.provider_type or self.provider_type or "").lower()
        return provider_type in {"feishu", "dingtalk"}

    def _validate_member_identifiers(self, provider: IdentityProviderRecord, user: ExternalUser) -> None:
        user.unionid = (user.unionid or "").strip()
        user.external_id = (user.external_id or "").strip()
        user.open_id = (user.open_id or "").strip()

        if self._provider_requires_unionid(provider) and not user.unionid:
            raise ValueError(
                f"unionid is required for {provider.provider_type} org sync user {user.external_id or user.name}"
            )

        if user.unionid and user.external_id and user.unionid == user.external_id:
            raise ValueError(
                f"invalid unionid for org sync user {user.external_id or user.name}: unionid must not equal external_id"
            )

    async def _find_existing_member(
        self,
        db: object | None,
        provider: IdentityProviderRecord,
        user: ExternalUser,
    ) -> OrgMemberRecord | None:
        del db
        if user.unionid:
            existing_member = await org_member_dao.get_by_unionid(provider.id, user.unionid)
            if existing_member:
                return existing_member

        return await org_member_dao.find_by_external_or_open_id(
            provider_id=provider.id,
            external_id=user.external_id or None,
            open_id=user.open_id or None,
            require_unionid_compatible=self._provider_requires_unionid(provider) and bool(user.unionid),
            unionid=user.unionid or None,
        )

    async def _resolve_platform_user(self, db: object | None, user: ExternalUser) -> UserRecord | None:
        """Resolve platform user from external user info."""
        del db
        email = _normalize_contact(user.email)
        if email:
            # Prefer tenant-scoped lookup when available
            found = await user_dao.get_by_email_and_tenant(email, self.tenant_id)
            if found:
                return found
            # Fall back to any tenant match via identity
            identity = await identity_dao.get_by_email(email)
            if identity:
                users = await user_dao.get_by_identity_id(identity.id)
                if users:
                    return users[0]

        mobile = _normalize_contact(user.mobile)
        if mobile:
            found = await user_dao.get_by_phone_and_tenant(mobile, self.tenant_id)
            if found:
                return found
            identity = await identity_dao.get_by_phone(mobile)
            if identity:
                users = await user_dao.get_by_identity_id(identity.id)
                if users:
                    return users[0]

        return None
