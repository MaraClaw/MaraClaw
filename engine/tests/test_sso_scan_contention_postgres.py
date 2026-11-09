import anyio
import pytest
from tests.sso_scan_contention_fixtures import ScanRace, admission_scenario, scan_race, wait_blocked

from app.dao.identity_provider_dao import identity_provider_dao
from app.db.connection import DbConnection
from app.db.session import connection_ctx
from app.records.identity import IdentityProviderRecord
from app.services.auth_provider import PROVIDER_CLASSES, BaseAuthProvider, ExternalUserInfo
from app.services.sso_scan_sessions import redeem_scan_session

__all__ = ["admission_scenario", "scan_race"]

pytestmark = pytest.mark.parametrize("provider_type", ["feishu", "dingtalk", "wecom"])


@pytest.mark.parametrize("pause_stage", ["exchange", "profile"])
@pytest.mark.parametrize(
    "change",
    ["active", "sso", "config", "type", "tenant", "delete", "restored"],
)
async def test_committed_revocation_during_network_leaves_no_writes(
    scan_race: ScanRace, change: str, pause_stage: str
) -> None:
    scenario = scan_race
    scenario.pause_stage = pause_stage
    before = await scenario.counts()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(scenario.callback)
            await scenario.entered.wait()
            statements = {
                "active": "UPDATE identity_providers SET is_active=false WHERE id=%(id)s",
                "sso": "UPDATE identity_providers SET sso_login_enabled=false WHERE id=%(id)s",
                "config": "UPDATE identity_providers SET config='{}'::jsonb WHERE id=%(id)s",
                "type": "UPDATE identity_providers SET provider_type='revoked' WHERE id=%(id)s",
                "tenant": "UPDATE identity_providers SET tenant_id=%(tenant)s WHERE id=%(id)s",
                "delete": "DELETE FROM identity_providers WHERE id=%(id)s",
                "restored": "UPDATE identity_providers SET is_active=false WHERE id=%(id)s",
            }
            await scenario.admission.db.execute(
                statements[change], {"id": scenario.provider.id, "tenant": scenario.admission.target_id}
            )
            if change == "restored":
                await scenario.admission.db.execute(
                    "UPDATE identity_providers SET is_active=true WHERE id=%(id)s",
                    {"id": scenario.provider.id},
                )
            scenario.release.set()
    assert len(scenario.responses) == 1
    assert "failed" in scenario.responses[0].lower()
    assert await scenario.counts() == before
    assert await scenario.scan_status() == "pending"


async def test_revocation_holding_provider_lock_wins(scan_race: ScanRace, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = scan_race
    before = await scenario.counts()
    locking = anyio.Event()
    backend_pid: list[object] = []
    original_lock = identity_provider_dao.get_for_update

    async def lock(db: DbConnection, provider_id: object) -> IdentityProviderRecord | None:
        backend_pid.append(await db.fetchval("SELECT pg_backend_pid()"))
        locking.set()
        return await original_lock(db, scenario.provider.id)

    monkeypatch.setattr(identity_provider_dao, "get_for_update", lock)
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(scenario.callback)
            await scenario.entered.wait()
            async with scenario.admission.db.raw.transaction():
                await scenario.admission.db.execute(
                    "UPDATE identity_providers SET is_active=false WHERE id=%(id)s", {"id": scenario.provider.id}
                )
                scenario.release.set()
                await locking.wait()
                await wait_blocked(scenario.admission.db, backend_pid[0])
                assert scenario.responses == []
    assert "failed" in scenario.responses[0].lower()
    assert await scenario.counts() == before
    assert await scenario.scan_status() == "pending"


async def test_finalization_holding_provider_lock_commits_before_revocation(
    scan_race: ScanRace, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scan_race
    resolved = anyio.Event()
    finish = anyio.Event()
    revoke_started = anyio.Event()
    revoked = anyio.Event()
    backend_pid: list[object] = []
    original_resolve = BaseAuthProvider.find_or_create_user

    async def resolve(adapter: BaseAuthProvider, info: ExternalUserInfo, tenant_id: str | None = None):
        user = await original_resolve(adapter, info, tenant_id)
        resolved.set()
        await finish.wait()
        return user

    async def revoke() -> None:
        backend_pid.append(await scenario.admission.db.fetchval("SELECT pg_backend_pid()"))
        revoke_started.set()
        await scenario.admission.db.execute(
            "UPDATE identity_providers SET is_active=false WHERE id=%(id)s", {"id": scenario.provider.id}
        )
        revoked.set()

    monkeypatch.setattr(PROVIDER_CLASSES[scenario.provider.provider_type], "find_or_create_user", resolve)
    scenario.release.set()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(scenario.callback)
            await resolved.wait()
            group.start_soon(revoke)
            await revoke_started.wait()
            async with connection_ctx() as observer:
                await wait_blocked(observer, backend_pid[0])
            assert not revoked.is_set()
            finish.set()
    assert revoked.is_set()
    assert "complete=1" in scenario.responses[0]
    assert await scenario.scan_status() == "authorized"
    assert await redeem_scan_session(scenario.scan.id) is not None
    assert await redeem_scan_session(scenario.scan.id) is None


async def test_duplicate_callbacks_have_one_authorization_and_one_account(scan_race: ScanRace) -> None:
    scenario = scan_race
    before = await scenario.counts()
    scenario.expected_exchanges = 2
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(scenario.callback)
            group.start_soon(scenario.callback)
            await scenario.entered.wait()
            scenario.release.set()
    assert sum("complete=1" in html for html in scenario.responses) == 1
    after = await scenario.counts()
    assert after["identities"] == before["identities"] + 1
    assert after["users"] == before["users"] + 1
    assert after["admissions"] == before["admissions"]
    assert await redeem_scan_session(scenario.scan.id) is not None
    assert await redeem_scan_session(scenario.scan.id) is None
