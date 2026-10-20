from copy import deepcopy
from uuid import uuid4

import pytest
from tests.test_google_workspace_transactions import GoogleFlow, callback, google_flow

from app.dao import identity_provider_dao, org_member_dao, user_dao
from app.records.org import OrgMemberRecord
from app.services.auth_provider import BaseAuthProvider, ExternalUserInfo, GoogleWorkspaceAuthProvider
from app.services.registration_service import registration_service

__all__ = ["google_flow"]


@pytest.mark.parametrize("change", ["active", "membership", "identity", "tenant", "missing"])
async def test_sso_checks_fresh_account_after_real_resolution(
    google_flow: GoogleFlow, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    cached_user = deepcopy(google_flow.actor)
    assert cached_user.identity is not None
    cached_user.identity_id = cached_user.identity.id
    current_user = deepcopy(cached_user)
    member = OrgMemberRecord(
        uuid4(), "Existing user", open_id="external", user_id=cached_user.id, tenant_id=cached_user.tenant_id
    )

    async def user_info(_self, _access_token):
        await google_flow.remote("profile")
        match change:
            case "membership":
                current_user.is_active = False
            case "identity":
                assert current_user.identity is not None
                current_user.identity.is_active = False
            case "tenant":
                current_user.tenant_id = uuid4()
        return ExternalUserInfo(
            provider_type="google_workspace", provider_user_id="external", raw_data={"open_id": "external"}
        )

    async def get_user(_user_id, *, fresh=False):
        return (None if change == "missing" else current_user) if fresh else cached_user

    async def preferred(_provider_type, _tenant_id):
        return google_flow.provider

    async def find_member(*_args):
        return member

    async def update_member(_member_id, _fields):
        await google_flow.write("member")
        return member

    async def web_provider(*_args, **_kwargs):
        return google_flow.provider

    monkeypatch.setattr(GoogleWorkspaceAuthProvider, "find_or_create_user", BaseAuthProvider.find_or_create_user)
    monkeypatch.setattr(GoogleWorkspaceAuthProvider, "get_user_info", user_info)
    monkeypatch.setattr(user_dao, "get_with_identity", get_user)
    monkeypatch.setattr(identity_provider_dao, "get_preferred", preferred)
    monkeypatch.setattr(org_member_dao, "find_active_by_provider_field", find_member)
    monkeypatch.setattr(org_member_dao, "get_by_user_and_provider", find_member)
    monkeypatch.setattr(org_member_dao, "update_fields", update_member)
    monkeypatch.setattr(registration_service, "ensure_identity_provider", web_provider)

    response = await callback(google_flow)

    if change == "active":
        assert "successful" in response.text
        assert google_flow.committed == ["member", "member", "scan"]
    else:
        assert "Auth failed:" in response.text
        assert google_flow.committed == []
        assert google_flow.pool.raw.rollbacks == 1
    assert google_flow.pool.active == 0
