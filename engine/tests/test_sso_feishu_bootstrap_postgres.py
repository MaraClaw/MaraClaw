import anyio
import pytest
from tests.sso_scan_contention_fixtures import ScanRace, admission_scenario, scan_race

from app.config import get_settings
from app.services.sso_scan_sessions import redeem_scan_session

__all__ = ["admission_scenario", "scan_race"]


@pytest.mark.parametrize("provider_type", ["feishu"])
@pytest.mark.parametrize("revoke_bootstrap", [False, True])
async def test_settings_bootstrap_is_captured_once_without_resurrection(
    scan_race: ScanRace, monkeypatch: pytest.MonkeyPatch, revoke_bootstrap: bool
) -> None:
    scenario = scan_race
    settings = get_settings()
    monkeypatch.setattr(settings, "FEISHU_APP_ID", "bootstrap-app")
    monkeypatch.setattr(settings, "FEISHU_APP_SECRET", "bootstrap-secret")
    await scenario.admission.db.execute("DELETE FROM identity_providers WHERE id=%(id)s", {"id": scenario.provider.id})
    before = await scenario.counts()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(scenario.callback)
            await scenario.entered.wait()
            assert scenario.provider_ids[0] != scenario.provider.id
            if revoke_bootstrap:
                await scenario.admission.db.execute(
                    "DELETE FROM identity_providers WHERE id=%(id)s", {"id": scenario.provider_ids[0]}
                )
            scenario.release.set()
    assert scenario.configs == ["bootstrap-secret"]
    if revoke_bootstrap:
        assert "failed" in scenario.responses[0].lower()
        assert await scenario.counts() == before
        assert await scenario.admission.db.fetchval("SELECT count(*) FROM identity_providers") == 0
        assert await scenario.scan_status() == "pending"
    else:
        assert "complete=1" in scenario.responses[0]
        assert await redeem_scan_session(scenario.scan.id) is not None
        assert await redeem_scan_session(scenario.scan.id) is None
