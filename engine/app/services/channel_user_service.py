"""Channel user resolution service for messaging platforms.

This service provides unified user resolution for incoming messages from
external channels (DingTalk, WeCom, Feishu, etc.). It reuses the SSO service
and OrgMember-based identity management.
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

from app.core.logging import logger
from app.dao import identity_dao, identity_provider_dao, org_member_dao, user_dao
from app.records.identity import IdentityProviderRecord
from app.records.org import OrgMemberRecord
from app.records.user import UserRecord
from app.services.sso_service import sso_service


class ChannelUserResolutionError(ValueError):
    """Raised when a channel message cannot be safely attributed to a user."""


class ChannelUserService:
    """Service for resolving channel users via OrgMember and SSO patterns."""

    CHANNEL_TYPE_ALIASES: ClassVar[dict[str, str]] = {
        "microsoft_teams": "teams",
    }

    def _normalize_channel_type(self, channel_type: str) -> str:
        raw = (channel_type or "").strip().lower()
        return self.CHANNEL_TYPE_ALIASES.get(raw, raw)

    def _legacy_provider_types_for_channel(self, channel_type: str) -> list[str]:
        normalized = self._normalize_channel_type(channel_type)
        legacy = [normalized]
        if normalized == "teams":
            legacy.append("microsoft_teams")
        elif normalized == "microsoft_teams":
            legacy.append("teams")
        return legacy

    def _get_channel_ids(
        self,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any],
    ) -> tuple[str | None, str | None, str | None]:
        normalized_channel = self._normalize_channel_type(channel_type)
        unionid = (extra_info.get("unionid") or extra_info.get("union_id") or "").strip() or None
        open_id = (extra_info.get("open_id") or "").strip() or None
        external_id = (extra_info.get("external_id") or external_user_id or "").strip() or None

        if normalized_channel == "feishu":
            external_id = (extra_info.get("external_id") or "").strip() or None
        elif normalized_channel == "dingtalk":
            open_id = open_id or None
        elif normalized_channel == "wecom":
            unionid = None
            open_id = open_id or None
        else:
            unionid = None
            open_id = None

        return unionid, open_id, external_id

    async def resolve_channel_user(
        self,
        agent: Any,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any] | None = None,
        db: Any = None,
    ) -> UserRecord:
        """Resolve channel user identity, find or create platform User."""
        del db
        tenant_id = agent.tenant_id
        tenant_id_text = str(tenant_id) if tenant_id is not None else None
        extra_info = extra_info or {}

        provider = await self._ensure_provider(None, channel_type, tenant_id)
        org_member = await self._find_org_member(None, provider.id, channel_type, external_user_id, extra_info)

        user: UserRecord | None = None

        if org_member and org_member.user_id:
            user = await user_dao.get_with_identity(org_member.user_id)
            if user:
                logger.debug(f"[{channel_type}] Found user via linked OrgMember: {user.id}")
                return user

        email = extra_info.get("email")
        mobile = extra_info.get("mobile")

        if not user and email:
            user = await sso_service.match_user_by_email(email, tenant_id_text)
            if user:
                logger.info(f"[{channel_type}] Matched user by email: {user.id}")

        if not user and mobile:
            user = await sso_service.match_user_by_mobile(mobile, tenant_id_text or "")
            if user:
                logger.info(f"[{channel_type}] Matched user by mobile: {user.id}")

        should_persist_member = True

        if user:
            if should_persist_member:
                if org_member and not org_member.user_id:
                    await org_member_dao.update(db_obj=org_member, obj_in={"user_id": user.id})
                elif not org_member:
                    existing_member = await self._find_existing_org_member_for_user(
                        None, user.id, provider.id, tenant_id
                    )
                    if existing_member:
                        unionid, open_id, external_id = self._get_channel_ids(
                            channel_type, external_user_id, extra_info
                        )
                        updates: dict[str, Any] = {}
                        if unionid and not existing_member.unionid:
                            updates["unionid"] = unionid
                        if open_id and not existing_member.open_id:
                            updates["open_id"] = open_id
                        if external_id and not existing_member.external_id:
                            updates["external_id"] = external_id
                        if updates:
                            await org_member_dao.update(db_obj=existing_member, obj_in=updates)
                        logger.info(
                            f"[{channel_type}] Reusing org-synced OrgMember {existing_member.id} "
                            f"for user {user.id} instead of creating a duplicate shell"
                        )
                    else:
                        await self._create_org_member_shell(
                            None, provider, channel_type, external_user_id, extra_info, linked_user_id=user.id
                        )
            return user

        unionid, open_id, external_id = self._get_channel_ids(channel_type, external_user_id, extra_info)

        if channel_type == "feishu" and not org_member and not (unionid or external_id):
            raise ChannelUserResolutionError(
                "Feishu sender could not be resolved to a stable user_id/union_id; "
                "refusing to lazily create a duplicate user from open_id only."
            )

        user = await self._create_channel_user(None, channel_type, external_user_id, extra_info, tenant_id)

        if should_persist_member:
            if org_member:
                await org_member_dao.update(db_obj=org_member, obj_in={"user_id": user.id})
            else:
                await self._create_org_member_shell(
                    None, provider, channel_type, external_user_id, extra_info, linked_user_id=user.id
                )
        logger.info(f"[{channel_type}] Created new user: {user.id} for external_id: {external_user_id}")

        return user

    async def _ensure_provider(
        self, db: Any, provider_type: str, tenant_id: uuid.UUID | None
    ) -> IdentityProviderRecord:
        """Get or create IdentityProvider record."""
        del db
        canonical_type = self._normalize_channel_type(provider_type)

        provider = await identity_provider_dao.get_by_type_and_tenant(canonical_type, tenant_id)
        if provider:
            return provider

        for legacy_type in self._legacy_provider_types_for_channel(provider_type):
            if legacy_type == canonical_type:
                continue
            legacy_provider = await identity_provider_dao.get_by_type_and_tenant(legacy_type, tenant_id)
            if legacy_provider:
                return legacy_provider

        return await identity_provider_dao.create(
            obj_in={
                "provider_type": canonical_type,
                "name": canonical_type.capitalize(),
                "is_active": True,
                "config": {},
                "tenant_id": tenant_id,
            }
        )

    async def _find_org_member(
        self,
        db: Any,
        provider_id: uuid.UUID,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any] | None = None,
    ) -> OrgMemberRecord | None:
        """Find OrgMember by external identity."""
        del db
        try:
            extra_info = extra_info or {}
            unionid, open_id, external_id = self._get_channel_ids(channel_type, external_user_id, extra_info)
            normalized_channel = self._normalize_channel_type(channel_type)

            if normalized_channel == "feishu":
                return await org_member_dao.find_active_by_any_ids(
                    provider_id=provider_id,
                    unionid=unionid,
                    open_id=open_id,
                    external_id=external_id,
                )
            if normalized_channel == "dingtalk":
                return await org_member_dao.find_active_by_any_ids(
                    provider_id=provider_id,
                    unionid=unionid,
                    external_id=external_id,
                )
            if normalized_channel == "wecom":
                if not external_id:
                    return None
                return await org_member_dao.find_active_by_any_ids(
                    provider_id=provider_id,
                    external_id=external_id,
                )
            if not external_id:
                return None
            return await org_member_dao.find_active_by_any_ids(
                provider_id=provider_id,
                external_id=external_id,
            )
        except Exception as e:
            logger.debug(f"[{channel_type}] OrgMember lookup failed: {e}")
            return None

    async def _create_org_member_shell(
        self,
        db: Any,
        provider: IdentityProviderRecord | Any,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any],
        linked_user_id: uuid.UUID | None = None,
    ) -> OrgMemberRecord:
        """Create a shell OrgMember record for this identity."""
        del db
        identity_seed = external_user_id or (extra_info.get("open_id") or "").strip() or uuid.uuid4().hex
        name = extra_info.get("name") or f"{channel_type.capitalize()} User {identity_seed[:8]}"
        unionid, open_id, external_id = self._get_channel_ids(channel_type, external_user_id, extra_info)

        return await org_member_dao.create(
            obj_in={
                "name": name,
                "email": extra_info.get("email"),
                "provider_id": provider.id,
                "user_id": linked_user_id,
                "tenant_id": provider.tenant_id,
                "external_id": external_id,
                "unionid": unionid,
                "open_id": open_id,
                "avatar_url": extra_info.get("avatar_url"),
                "phone": extra_info.get("mobile"),
                "title": extra_info.get("title", ""),
                "status": "active",
            }
        )

    async def _find_existing_org_member_for_user(
        self,
        db: Any,
        user_id: uuid.UUID,
        provider_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
    ) -> OrgMemberRecord | None:
        del db
        return await org_member_dao.get_active_for_user_and_provider(
            user_id=user_id,
            provider_id=provider_id,
            tenant_id=tenant_id,
        )

    async def _create_channel_user(
        self,
        db: Any,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any],
        tenant_id: uuid.UUID | None,
    ) -> UserRecord:
        """Create a new Identity + User for channel identity (lazy registration)."""
        del db
        email = extra_info.get("email")
        mobile = extra_info.get("mobile")
        identity_seed = external_user_id or (extra_info.get("open_id") or "").strip() or uuid.uuid4().hex
        name = extra_info.get("name") or f"{channel_type.capitalize()} {identity_seed[:8]}"

        username = email.split("@")[0] if email else f"{channel_type}_{identity_seed[:12]}"

        existing = await user_dao.get_by_identity_username(username)
        if existing and (tenant_id is None or existing.tenant_id == tenant_id):
            username = f"{username}_{identity_seed[:6]}"

        email = email or f"{username}@{channel_type}.local"

        identity = None
        found = await identity_dao.get_by_email(email)
        if found:
            identity = found
        elif mobile:
            found = await identity_dao.get_by_phone(mobile)
            if found:
                identity = found

        if not identity:
            identity = await identity_dao.create_identity(
                email=email,
                phone=mobile,
                username=username,
                password_hash=None,
                is_platform_admin=False,
                email_verified=True,
            )

        user = await user_dao.create(
            obj_in={
                "identity_id": identity.id,
                "display_name": name,
                "avatar_url": extra_info.get("avatar_url"),
                "role": "member",
                "registration_source": channel_type,
                "tenant_id": tenant_id,
                "is_active": True,
            }
        )
        loaded = await user_dao.get_with_identity(user.id)
        return loaded or user


channel_user_service = ChannelUserService()


async def get_platform_user_by_org_member(
    org_member: OrgMemberRecord | Any,
    agent_tenant_id: uuid.UUID | None = None,
    db: Any = None,
) -> UserRecord:
    """Get or create platform User from an existing OrgMember."""
    del db
    agent_tenant_id_text = str(agent_tenant_id) if agent_tenant_id is not None else None

    if org_member.user_id:
        user = await user_dao.get_with_identity(org_member.user_id)
        if user and (agent_tenant_id is None or user.tenant_id == agent_tenant_id):
            return user

    user = None
    if org_member.email:
        user = await sso_service.match_user_by_email(org_member.email, agent_tenant_id_text)
    if not user and org_member.phone:
        user = await sso_service.match_user_by_mobile(org_member.phone, agent_tenant_id_text or "")

    if user:
        await org_member_dao.update(db_obj=org_member, obj_in={"user_id": user.id})
        loaded = await user_dao.get_with_identity(user.id)
        return loaded or user

    provider = await identity_provider_dao.get(org_member.provider_id) if org_member.provider_id else None
    channel_type = provider.provider_type if provider else "unknown"
    external_seed = org_member.external_id

    email = org_member.email
    seed_for_name = external_seed or org_member.id.hex
    name = org_member.name or f"{channel_type.capitalize()} User {seed_for_name[:8]}"

    if email:
        username = email.split("@")[0]
    elif external_seed:
        username = f"{channel_type}_{external_seed[:12]}"
    else:
        username = f"{channel_type}_{org_member.id.hex[:12]}"

    existing = await user_dao.get_by_identity_username(username)
    if existing and (agent_tenant_id is None or existing.tenant_id == agent_tenant_id):
        username = f"{username}_{external_seed[:6] if external_seed else org_member.id.hex[:6]}"

    email = email or f"{username}@{channel_type}.local"

    identity = await identity_dao.get_by_email(email)
    if not identity and org_member.phone:
        identity = await identity_dao.get_by_phone(org_member.phone)

    if not identity:
        identity = await identity_dao.create_identity(
            email=email,
            phone=org_member.phone,
            username=username,
            password_hash=None,
            is_platform_admin=False,
            email_verified=True,
        )

    user = await user_dao.create(
        obj_in={
            "identity_id": identity.id,
            "display_name": name,
            "avatar_url": org_member.avatar_url,
            "role": "member",
            "registration_source": channel_type,
            "tenant_id": agent_tenant_id,
            "is_active": True,
        }
    )
    await org_member_dao.update(db_obj=org_member, obj_in={"user_id": user.id})
    logger.info(f"[channel_user_service] Created User {user.id} for OrgMember {org_member.id} ({name})")
    loaded = await user_dao.get_with_identity(user.id)
    return loaded or user
