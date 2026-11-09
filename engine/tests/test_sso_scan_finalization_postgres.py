from uuid import UUID

import anyio
import pytest
from tests.sso_scan_contention_fixtures import ScanRace, admission_scenario, scan_race

from app.core.json_types import int_from_row
from app.dao.identity_provider_dao import identity_provider_dao
from app.dao.org_member_dao import org_member_dao
from app.dao.sso_scan_session_dao import sso_scan_session_dao
from app.db.session import get_connection
from app.records.sso_scan_session import SSOScanSessionRecord
from app.services.sso_scan_sessions import redeem_scan_session

__all__ = ["admission_scenario", "scan_race"]

pytestmark = pytest.mark.parametrize("provider_type", ["feishu", "dingtalk", "wecom"])


@pytest.mark.parametrize("existing_link", [False, True])
async def test_duplicate_provider_cannot_redirect_lookup_or_link(
    scan_race: ScanRace, monkeypatch: pytest.MonkeyPatch, existing_link: bool
) -> None:
    scenario = scan_race
    scenario.info.email = "applicant@acme.test"
    scenario.info.raw_data["union_id"] = "shared-union-id"
    if existing_link:
        await org_member_dao.create(
            obj_in={
                "provider_id": scenario.provider.id,
                "user_id": scenario.admission.applicant.id,
                "name": scenario.admission.applicant.display_name,
                "tenant_id": scenario.admission.source_id,
                "external_id": "previous-external-id",
                "unionid": "shared-union-id",
            }
        )
    original_preferred = identity_provider_dao.get_preferred

    async def preferred(provider_type: str, tenant_id: UUID | None = None):
        assert provider_type != scenario.provider.provider_type, "bound login reselected preferred provider"
        return await original_preferred(provider_type, tenant_id)

    monkeypatch.setattr(identity_provider_dao, "get_preferred", preferred)
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(scenario.callback)
            await scenario.entered.wait()
            duplicate = await identity_provider_dao.create(
                obj_in={
                    "provider_type": scenario.provider.provider_type,
                    "name": "New preferred row",
                    "is_active": True,
                    "sso_login_enabled": True,
                    "config": {"app_secret": "different-authority"},
                }
            )
            await org_member_dao.create(
                obj_in={
                    "provider_id": duplicate.id,
                    "user_id": scenario.admission.outsider.id,
                    "name": scenario.admission.outsider.display_name,
                    "tenant_id": scenario.admission.source_id,
                    "external_id": "exact-external-id",
                    "unionid": "shared-union-id",
                }
            )
            scenario.release.set()
    assert "complete=1" in scenario.responses[0]
    assert scenario.configs == ["captured-secret"]
    assert (
        await scenario.admission.db.fetchval(
            "SELECT user_id FROM sso_scan_sessions WHERE id=%(id)s", {"id": scenario.scan.id}
        )
        == scenario.admission.applicant.id
    )
    assert (
        await scenario.admission.db.fetchval(
            "SELECT user_id FROM org_members WHERE provider_id=%(id)s AND "
            "((%(by_external)s AND external_id='exact-external-id') "
            "OR (NOT %(by_external)s AND unionid='shared-union-id'))",
            {
                "id": scenario.provider.id,
                "by_external": scenario.provider.provider_type == "wecom" and not existing_link,
            },
        )
        == scenario.admission.applicant.id
    )


async def test_lost_scan_transition_rolls_back_new_identity_member_and_admission(
    scan_race: ScanRace, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scan_race
    scenario.info.email = "new-scan@acme.test"
    before = await scenario.counts()
    original_authorize = sso_scan_session_dao.authorize
    reached_authorization: list[bool] = []

    async def authorize(
        session_id: UUID, *, user_id: UUID, provider_type: str, tenant_id: UUID | None
    ) -> SSOScanSessionRecord | None:
        db = get_connection()
        assert db is not None
        assert await db.fetchval("SELECT count(*) FROM identities") == before["identities"] + 1
        assert await db.fetchval("SELECT count(*) FROM users") == before["users"] + 1
        assert int_from_row(await db.fetchval("SELECT count(*) FROM org_members")) > before["members"]
        assert await db.fetchval("SELECT count(*) FROM company_admission_requests") == before["admissions"] + 1
        reached_authorization.append(True)
        await scenario.admission.db.execute(
            "UPDATE sso_scan_sessions SET status='completed' WHERE id=%(id)s", {"id": session_id}
        )
        return await original_authorize(session_id, user_id=user_id, provider_type=provider_type, tenant_id=tenant_id)

    monkeypatch.setattr(sso_scan_session_dao, "authorize", authorize)
    scenario.release.set()
    await scenario.callback()
    assert reached_authorization == [True]
    assert "failed" in scenario.responses[0].lower()
    assert await scenario.counts() == before
    assert await scenario.scan_status() == "completed"
    assert await redeem_scan_session(scenario.scan.id) is None


@pytest.mark.parametrize("changed_tenant", ["provider", "scan"])
async def test_changed_tenant_cannot_authorize(scan_race: ScanRace, changed_tenant: str) -> None:
    scenario = scan_race
    before = await scenario.counts()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(scenario.callback)
            await scenario.entered.wait()
            if changed_tenant == "provider":
                await scenario.admission.db.execute(
                    "UPDATE identity_providers SET tenant_id=%(tenant)s WHERE id=%(id)s",
                    {"tenant": scenario.admission.target_id, "id": scenario.provider.id},
                )
            else:
                await scenario.admission.db.execute(
                    "UPDATE sso_scan_sessions SET tenant_id=%(tenant)s WHERE id=%(id)s",
                    {"tenant": scenario.admission.target_id, "id": scenario.scan.id},
                )
            scenario.release.set()
    assert "failed" in scenario.responses[0].lower()
    assert await scenario.counts() == before
    assert await scenario.scan_status() == "pending"
