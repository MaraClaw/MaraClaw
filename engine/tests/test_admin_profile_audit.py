from collections.abc import AsyncIterator

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import admin, organization, users
from app.dao.admin_audit_dao import admin_audit_log_dao
from app.dao.org_member_dao import org_member_dao
from app.dao.user_dao import user_dao
from app.db.errors import CheckViolationError
from app.db.session import bind_crud_connection, connection_ctx
from app.records.org import OrgMemberRecord
from app.schemas.schemas import UserUpdate
from app.services.registration_service import registration_service
from company_admission_fixtures import AdmissionScenario, admission_scenario

__all__ = ["admission_scenario"]


@pytest.fixture
async def profile_client(admission_scenario: AdmissionScenario) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    for router in (organization.router, admin.router, users.router):
        app.include_router(router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://profiles.test") as client:
        yield client


@pytest.fixture
async def profile_member(admission_scenario: AdmissionScenario) -> OrgMemberRecord:
    target = admission_scenario.additional
    provider = await registration_service.ensure_identity_provider("web", target.tenant_id, name="Platform")
    return await org_member_dao.create(
        obj_in={
            "name": target.display_name,
            "tenant_id": target.tenant_id,
            "provider_id": provider.id,
            "user_id": target.id,
            "email": target.email,
        }
    )


async def test_profile_http_persists_allowlisted_diff_in_target_tenant(
    admission_scenario: AdmissionScenario, profile_client: AsyncClient, profile_member: OrgMemberRecord
):
    # Given a platform admin editing a member in a different company.
    scenario = admission_scenario
    target = scenario.additional

    # When profile and contact fields change alongside a credential-bearing avatar URL.
    response = await profile_client.patch(
        f"/api/org/users/{target.id}",
        json={
            "display_name": "Edited member",
            "title": "Operations",
            "email": "edited@example.com",
            "primary_mobile": "123456789",
            "avatar_url": "https://avatar-user:avatar-password@images.test/member?token=avatar-secret",
            "password_hash": "ignored-secret",
        },
        headers=scenario.headers(scenario.outsider),
    )

    # Then the unchanged API surface and company trail expose only the safe profile diff.
    assert response.status_code == 200, response.text
    assert response.json()["email"] == "edited@example.com"
    assert await scenario.db.fetchone(
        "SELECT email,phone FROM org_members WHERE id=%(id)s", {"id": profile_member.id}
    ) == {
        "email": "edited@example.com",
        "phone": "123456789",
    }
    events = await admin_audit_log_dao.list_recent(tenant_id=scenario.target_id)
    assert len(events) == 1
    event = events[0]
    assert (event.actor_id, event.target_id, event.tenant_id) == (scenario.outsider.id, target.id, scenario.target_id)
    assert (event.action, event.target_type) == ("user.profile_updated", "user")
    assert event.changes == {
        "display_name": {"before": "additional", "after": "Edited member"},
        "title": {"before": None, "after": "Operations"},
        "email": {"before": "additional@acme.test", "after": "edited@example.com"},
        "primary_mobile": {"before": None, "after": "123456789"},
    }
    assert event.details == {"avatar_changed": True}
    assert await admin_audit_log_dao.list_recent(tenant_id=scenario.source_id) == []


async def test_direct_profile_update_rolls_back_all_writes_on_contact_sync_failure(
    admission_scenario: AdmissionScenario, profile_member: OrgMemberRecord
):
    # Given a real constraint failure after both profile and identity writes.
    scenario = admission_scenario
    await scenario.db.execute(
        "ALTER TABLE org_members ADD CONSTRAINT reject_profile_contact CHECK (email <> 'edited@example.com')"
    )

    # When the handler is called directly, with no request transaction owner.
    with pytest.raises(CheckViolationError):
        await organization.admin_update_user(
            scenario.additional.id,
            UserUpdate(display_name="Edited member", email="edited@example.com"),
            scenario.outsider,
        )

    # Then the handler's own transaction rolls back the complete multi-write operation.
    target = await user_dao.get_with_identity(scenario.additional.id, fresh=True)
    assert target is not None
    assert (target.display_name, target.email) == ("additional", "additional@acme.test")
    assert await scenario.db.fetchval("SELECT email FROM org_members WHERE id=%(id)s", {"id": profile_member.id}) == (
        "additional@acme.test"
    )
    assert await scenario.db.fetchval("SELECT count(*) FROM admin_audit_logs") == 0


async def test_profile_audit_joins_outer_transaction_and_rolls_back(admission_scenario: AdmissionScenario):
    # Given an outer transaction that will fail after the profile audit is inserted.
    scenario = admission_scenario

    async def abort_after_update() -> None:
        async with connection_ctx() as db:
            await organization.admin_update_user(
                scenario.additional.id, UserUpdate(title="Operations"), scenario.outsider
            )
            assert await db.fetchval("SELECT count(*) FROM admin_audit_logs") == 1
            assert await scenario.db.fetchval("SELECT count(*) FROM admin_audit_logs") == 0
            assert (
                await scenario.db.fetchval("SELECT title FROM users WHERE id=%(id)s", {"id": scenario.additional.id})
                is None
            )
            raise RuntimeError("outer profile failure")

    # When the direct handler joins that transaction instead of committing independently.
    with pytest.raises(RuntimeError, match="outer profile failure"):
        await abort_after_update()

    # Then neither the profile mutation nor its audit survives.
    assert await scenario.db.fetchval("SELECT title FROM users WHERE id=%(id)s", {"id": scenario.additional.id}) is None
    assert await scenario.db.fetchval("SELECT count(*) FROM admin_audit_logs") == 0


async def test_profile_update_survives_real_audit_sql_failure(
    admission_scenario: AdmissionScenario, profile_member: OrgMemberRecord
):
    # Given a database constraint that rejects the audit write, not the profile writes.
    scenario = admission_scenario
    await scenario.db.execute(
        "ALTER TABLE admin_audit_logs ADD CONSTRAINT reject_profile_audit CHECK (action <> 'user.profile_updated')"
    )

    # When the best-effort audit fails inside the handler's transaction.
    result = await organization.admin_update_user(
        scenario.additional.id,
        UserUpdate(display_name="Edited member", email="edited@example.com"),
        scenario.outsider,
    )

    # Then the successful user, identity and member-contact writes still commit.
    assert (result.display_name, result.email) == ("Edited member", "edited@example.com")
    target = await user_dao.get_with_identity(scenario.additional.id, fresh=True)
    assert target is not None
    assert (target.display_name, target.email) == ("Edited member", "edited@example.com")
    assert await scenario.db.fetchval("SELECT email FROM org_members WHERE id=%(id)s", {"id": profile_member.id}) == (
        "edited@example.com"
    )
    assert await scenario.db.fetchval("SELECT count(*) FROM admin_audit_logs") == 0


async def test_foreign_org_admin_profile_rejection_has_no_audit(
    admission_scenario: AdmissionScenario, profile_client: AsyncClient
):
    # Given an org admin addressing a member of a different tenant.
    scenario = admission_scenario

    # When the unchanged authorization boundary rejects the HTTP edit.
    response = await profile_client.patch(
        f"/api/org/users/{scenario.applicant.id}",
        json={"display_name": "Forbidden"},
        headers=scenario.headers(scenario.genesis),
    )

    # Then the original not-found response is preserved without a write or event.
    assert response.status_code == 404
    assert (await scenario.current_applicant()).display_name == "applicant"
    assert await scenario.db.fetchval("SELECT count(*) FROM admin_audit_logs") == 0


@pytest.mark.parametrize("avatar", [False, True])
async def test_profile_audit_records_avatar_indicator_without_url_or_noop_event(
    admission_scenario: AdmissionScenario, avatar: bool
):
    # Given an edit that leaves the safe profile fields unchanged.
    scenario = admission_scenario
    data = UserUpdate(avatar_url="https://user:password@images.test/avatar?token=secret") if avatar else UserUpdate()

    # When only an avatar is changed, or the entire request is a no-op.
    await organization.admin_update_user(scenario.additional.id, data, scenario.outsider)

    # Then only the avatar change has an event, containing no URL material.
    events = await admin_audit_log_dao.list_recent(tenant_id=scenario.target_id)
    assert len(events) == int(avatar)
    if avatar:
        assert events[0].changes == {}
        assert events[0].details == {"avatar_changed": True}


@pytest.mark.parametrize("platform", [False, True])
async def test_profile_audit_page_matches_legacy_array_with_safe_changes(admission_scenario, profile_client, platform):
    # Given a successful profile edit with safe and sensitive input fields.
    scenario = admission_scenario
    await organization.admin_update_user(
        scenario.additional.id,
        UserUpdate(display_name="Changed profile", avatar_url="https://user:secret@images.test/avatar?token=secret"),
        scenario.outsider,
    )
    path = "/api/admin/audit-logs" if platform else "/api/users/admin-audit-logs"
    viewer = scenario.outsider if platform else scenario.genesis
    params = {"tenant_id": str(scenario.target_id), "action": "user.profile_updated"}
    # When an authorized administrator opens the additive page surface.
    page = await profile_client.get(path + "/page", params=params, headers=scenario.headers(viewer))
    legacy = await profile_client.get(path, params=params, headers=scenario.headers(viewer))
    # Then it exposes the existing safe event shape, not the sensitive profile input.
    assert page.status_code == legacy.status_code == 200
    assert page.json() == {"items": legacy.json(), "next_cursor": None}
    assert len(page.json()["items"]) == 1
    assert page.json()["items"][0]["changes"] == {"display_name": {"before": "additional", "after": "Changed profile"}}
    assert page.json()["items"][0]["details"] == {"avatar_changed": True}
