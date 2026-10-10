from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.config import get_settings
from app.records.identity import IdentityProviderRecord, IdentityRecord
from app.records.user import UserRecord
from app.services import onboarding, platform_admin_seeder as seeder
from app.services.auth_provider import BaseAuthProvider, ExternalUserInfo, FeishuAuthProvider
from app.services.channel_user_service import ChannelUserService
from app.services.channels.types import outbound_provider_key


@pytest.mark.parametrize("owner", [BaseAuthProvider, FeishuAuthProvider])
@pytest.mark.parametrize(
    "name", ["_find_user_by_legacy_fields", "_update_legacy_user_fields", "_set_legacy_user_fields"]
)
def test_obsolete_provider_extension_contract_is_absent(owner: type[BaseAuthProvider], name: str) -> None:
    # Given / When / Then: retired extensions are not exposed by either provider class.
    assert not hasattr(owner, name)


def test_obsolete_onboarding_completion_alias_is_absent() -> None:
    # Given / When / Then: callers must use the canonical phase API.
    assert not hasattr(onboarding, "mark_onboarded")


@pytest.mark.parametrize("phase", ["greeted", "custom_style", "custom_boundaries", "template_focus", "completed"])
async def test_canonical_onboarding_phase_is_persisted(phase: str) -> None:
    # Given
    agent_id, user_id = uuid4(), uuid4()
    execute = AsyncMock()
    with patch("app.db.session.connection_ctx") as context:
        context.return_value.__aenter__.return_value.execute = execute
        # When
        await onboarding.mark_onboarding_phase(agent_id, user_id, phase)
    # Then
    assert execute.await_args is not None
    assert execute.await_args.args[1] == {"agent_id": agent_id, "user_id": user_id, "phase": phase}


@pytest.mark.parametrize("raw", ["google_chat", "googlechat", "google-chat", "gchat"])
async def test_google_chat_input_alias_does_not_reuse_obsolete_provider_rows(raw: str) -> None:
    # Given
    tenant_id = uuid4()
    obsolete = IdentityProviderRecord(id=uuid4(), provider_type="gchat", name="Obsolete", tenant_id=tenant_id)
    canonical = IdentityProviderRecord(id=uuid4(), provider_type="google_chat", name="Chat", tenant_id=tenant_id)
    lookup = AsyncMock(side_effect=[None, obsolete])
    create = AsyncMock(return_value=canonical)
    with (
        patch("app.services.channel_user_service.identity_provider_dao.get_by_type_and_tenant", lookup),
        patch("app.services.channel_user_service.identity_provider_dao.create", create),
    ):
        # When
        result = await ChannelUserService()._ensure_provider(raw, tenant_id)
    # Then
    assert result is canonical
    lookup.assert_awaited_once_with("google_chat", tenant_id)
    assert create.await_args is not None
    assert create.await_args.kwargs["obj_in"]["provider_type"] == "google_chat"


@pytest.mark.parametrize("raw", ["teams", "microsoft_teams", "ms_teams", "msteams"])
@pytest.mark.parametrize("persisted", ["teams", "microsoft_teams"])
async def test_teams_current_provider_writers_remain_resolvable(raw: str, persisted: str) -> None:
    # Given
    tenant_id = uuid4()
    provider = IdentityProviderRecord(id=uuid4(), provider_type=persisted, name="Teams", tenant_id=tenant_id)
    lookup = AsyncMock(side_effect=[provider] if persisted == "teams" else [None, provider])
    create = AsyncMock()
    with (
        patch("app.services.channel_user_service.identity_provider_dao.get_by_type_and_tenant", lookup),
        patch("app.services.channel_user_service.identity_provider_dao.create", create),
    ):
        # When
        result = await ChannelUserService()._ensure_provider(raw, tenant_id)
    # Then
    assert result is provider
    assert outbound_provider_key(raw) == "teams"
    create.assert_not_awaited()




async def test_provider_profile_update_keeps_real_identity_and_membership_persistence() -> None:
    # Given
    identity = IdentityRecord(id=uuid4())
    user = UserRecord(id=uuid4(), identity_id=identity.id, identity=identity)
    info = ExternalUserInfo(provider_type="feishu", name="Ada", email="ada@example.com", mobile="123")
    identity_update = AsyncMock(return_value=identity)
    user_update = AsyncMock(return_value=user)
    with (
        patch("app.services.auth_provider.identity_dao.update", identity_update),
        patch("app.services.auth_provider.user_dao.update", user_update),
    ):
        # When
        result = await FeishuAuthProvider()._update_existing_user(user, info)
    # Then
    assert result is user
    identity_update.assert_awaited_once_with(db_obj=identity, obj_in={"email": "ada@example.com", "phone": "123"})
    user_update.assert_awaited_once_with(db_obj=user, obj_in={"display_name": "Ada"})


async def test_provider_creation_keeps_identity_link_and_member_role() -> None:
    # Given
    identity = IdentityRecord(id=uuid4(), email="ada@example.com")
    user = UserRecord(id=uuid4(), identity_id=identity.id)
    info = ExternalUserInfo(provider_type="feishu", provider_union_id="union-123", email="ada@example.com", name="Ada")
    create = AsyncMock(return_value=user)
    with (
        patch(
            "app.services.registration_service.registration_service.find_or_create_identity",
            AsyncMock(return_value=identity),
        ),
        patch("app.services.auth_provider.identity_dao.is_username_taken", AsyncMock(return_value=False)),
        patch("app.services.auth_provider.user_dao.create", create),
    ):
        # When
        result = await FeishuAuthProvider()._create_new_user(info, None)
    # Then
    assert result.identity is identity
    assert create.await_args is not None
    fields = create.await_args.kwargs["obj_in"]
    assert fields["identity_id"] == identity.id
    assert fields["role"] == "member"
    assert fields["registration_source"] == "feishu"
