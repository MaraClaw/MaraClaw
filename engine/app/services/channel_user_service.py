"""Channel identity lookup for messaging platforms.

``ChannelUserService`` finds the OrgMember a channel identity is linked to. Inbound senders are attributed only through
a linked, active member (``channels.binding.resolve_bound_sender``). ``linked_platform_user`` serves outbound tools and
permission candidates by finding the user an OrgMember already belongs to. No channel or org-member path creates a user,
an identity or a member: a member with no linked or matching user has no platform user.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import ClassVar

from app.core.json_types import JsonObject, json_as_str, json_object_from
from app.dao import org_member_dao, user_dao
from app.records.org import OrgMemberRecord
from app.records.user import UserRecord
from app.services.sso_service import sso_service


def _extra_text(info: JsonObject, key: str) -> str | None:
    text = json_as_str(info.get(key))
    if text is None:
        return None
    text = text.strip()
    return text or None


class ChannelUserResolutionError(ValueError):
    """Raised when a channel message cannot be safely attributed to a user."""


async def _require_company_membership(user: UserRecord, tenant_id: uuid.UUID | None) -> None:
    from app.dao.company_admission_dao import company_admission_dao

    if tenant_id is None or user.tenant_id == tenant_id:
        return
    tenant = await company_admission_dao.tenant(tenant_id)
    if tenant is not None and not tenant.is_system and not tenant.is_default_end_user_org:
        raise ChannelUserResolutionError("Company admission must be approved through the web application first.")


class ChannelUserService:
    """Locates the OrgMember linked to a channel identity; never creates a user or a member."""

    CHANNEL_TYPE_ALIASES: ClassVar[dict[str, str]] = {
        "microsoft_teams": "teams",
        "googlechat": "google_chat",
        "google-chat": "google_chat",
        "gchat": "google_chat",
    }

    def _normalize_channel_type(self, channel_type: str) -> str:
        from app.services.channels.types import normalize_channel_type

        stored = normalize_channel_type(channel_type) or (channel_type or "").strip().lower()
        # Preserve historical short name used in identity provider rows for Teams.
        if stored == "microsoft_teams":
            return "teams"
        return self.CHANNEL_TYPE_ALIASES.get(stored, stored)

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
        extra_info: Mapping[str, object],
    ) -> tuple[str | None, str | None, str | None]:
        info = json_object_from(extra_info)
        normalized_channel = self._normalize_channel_type(channel_type)
        unionid = _extra_text(info, "unionid") or _extra_text(info, "union_id")
        open_id = _extra_text(info, "open_id")
        external_id = _extra_text(info, "external_id") or (external_user_id or "").strip() or None

        if normalized_channel == "feishu":
            external_id = _extra_text(info, "external_id")
        elif normalized_channel == "dingtalk":
            open_id = open_id or None
        elif normalized_channel == "wecom":
            unionid = None
            open_id = open_id or None
        else:
            unionid = None
            open_id = None

        return unionid, open_id, external_id

    async def _find_org_member(
        self,
        provider_id: uuid.UUID,
        channel_type: str,
        external_user_id: str | None,
        extra_info: Mapping[str, object] | None = None,
    ) -> OrgMemberRecord | None:
        """Find OrgMember by external identity; a failed lookup raises, it never reads as an unlinked sender."""
        extra_info = json_object_from(extra_info)
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
            if external_id in (None, ""):
                return None
            return await org_member_dao.find_active_by_any_ids(
                provider_id=provider_id,
                external_id=external_id,
            )
        if external_id in (None, ""):
            return None
        return await org_member_dao.find_active_by_any_ids(
            provider_id=provider_id,
            external_id=external_id,
        )


channel_user_service = ChannelUserService()


async def linked_platform_user(
    org_member: OrgMemberRecord,
    agent_tenant_id: uuid.UUID | None = None,
) -> UserRecord | None:
    """Return the platform user an OrgMember belongs to, or None when it has none; never creates one.

    A member already linked to a user in the agent's tenant resolves to it. Otherwise an existing user with the member's
    email or phone is linked to the member. An unlinked member that matches no user stays unlinked.
    """
    agent_tenant_id_text = str(agent_tenant_id) if agent_tenant_id is not None else None

    if org_member.user_id:
        user = await user_dao.get_with_identity(org_member.user_id)
        if user and (agent_tenant_id is None or user.tenant_id == agent_tenant_id):
            await _require_company_membership(user, agent_tenant_id or org_member.tenant_id)
            return user

    user = None
    if org_member.email is not None and org_member.email:
        user = await sso_service.match_user_by_email(org_member.email, agent_tenant_id_text)
    if not user and org_member.phone is not None and org_member.phone:
        user = await sso_service.match_user_by_mobile(org_member.phone, agent_tenant_id_text or "")
    if not user:
        return None

    await _require_company_membership(user, agent_tenant_id or org_member.tenant_id)
    _ = await org_member_dao.update(db_obj=org_member, obj_in={"user_id": user.id})
    loaded = await user_dao.get_with_identity(user.id)
    return loaded or user
