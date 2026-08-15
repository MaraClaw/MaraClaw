from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import okr as okr_api
from app.records.okr import OKRKeyResultRecord, OKRObjectiveRecord, OKRSettingsRecord
from app.records.user import UserRecord


TENANT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
AGENT_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
MEMBER_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
OBJECTIVE_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
KR_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")


def _admin() -> UserRecord:
    return UserRecord(id=USER_ID, tenant_id=TENANT_ID, role="org_admin", display_name="Admin")


def _member() -> UserRecord:
    return UserRecord(id=USER_ID, tenant_id=TENANT_ID, role="member", display_name="Member")


def _settings(**overrides: object) -> OKRSettingsRecord:
    values: dict[str, object] = {
        "tenant_id": TENANT_ID,
        "enabled": False,
        "first_enabled_at": None,
        "daily_report_enabled": False,
        "daily_report_time": "18:00",
        "daily_report_skip_non_workdays": True,
        "weekly_report_enabled": True,
        "weekly_report_day": 4,
        "period_frequency": "quarterly",
        "period_length_days": None,
        "okr_agent_id": None,
    }
    values.update(overrides)
    return OKRSettingsRecord(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_settings_forces_weekly_flags_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(okr_api, "_get_or_create_settings", AsyncMock(return_value=_settings()))

    result = await okr_api.get_okr_settings(user=_admin())

    assert result.weekly_report_enabled is False
    assert result.weekly_report_day == 0
    assert result.period_frequency_locked is False


@pytest.mark.asyncio
async def test_update_settings_rejects_non_admin() -> None:
    with pytest.raises(HTTPException) as exc:
        await okr_api.update_okr_settings(okr_api.OKRSettingsUpdate(enabled=True), user=_member())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_update_settings_enable_syncs_triggers_and_seeds(monkeypatch: pytest.MonkeyPatch) -> None:
    initial = _settings()
    updated = _settings(enabled=True, first_enabled_at=datetime(2026, 1, 1, tzinfo=UTC), okr_agent_id=None)
    seeded = _settings(enabled=True, first_enabled_at=datetime(2026, 1, 1, tzinfo=UTC), okr_agent_id=AGENT_ID)
    monkeypatch.setattr(okr_api, "_get_or_create_settings", AsyncMock(side_effect=[initial, seeded]))
    monkeypatch.setattr(okr_api.okr_settings_dao, "update", AsyncMock(return_value=updated))
    sync = AsyncMock()
    monkeypatch.setattr(okr_api, "_sync_okr_report_triggers", sync)
    seed = AsyncMock()
    monkeypatch.setattr("app.services.agent_seeder.seed_okr_agent_for_tenant", seed)

    result = await okr_api.update_okr_settings(okr_api.OKRSettingsUpdate(enabled=True), user=_admin())

    assert result.enabled is True
    assert result.weekly_report_enabled is False
    assert result.okr_agent_id == str(AGENT_ID)
    assert result.period_frequency_locked is True
    assert sync.await_count == 2
    seed.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_settings_locks_period_after_first_enable(monkeypatch: pytest.MonkeyPatch) -> None:
    locked = _settings(enabled=True, first_enabled_at=datetime(2026, 1, 1, tzinfo=UTC), okr_agent_id=AGENT_ID)
    monkeypatch.setattr(okr_api, "_get_or_create_settings", AsyncMock(return_value=locked))

    with pytest.raises(HTTPException) as exc:
        await okr_api.update_okr_settings(okr_api.OKRSettingsUpdate(period_frequency="monthly"), user=_admin())
    assert exc.value.status_code == 400
    assert "locked" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_sync_relationships_wipes_and_rebuilds(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(enabled=True, okr_agent_id=AGENT_ID)
    monkeypatch.setattr(okr_api, "_get_or_create_settings", AsyncMock(return_value=settings))
    sync = AsyncMock()
    monkeypatch.setattr(okr_api, "_sync_okr_agent_relationships", sync)

    result = await okr_api.sync_okr_relationships(user=_admin())

    assert result == {"status": "ok", "okr_agent_id": str(AGENT_ID)}
    sync.assert_awaited_once_with(TENANT_ID, AGENT_ID)


@pytest.mark.asyncio
async def test_create_objective_maps_org_member_to_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    member = SimpleNamespace(id=MEMBER_ID, user_id=USER_ID)
    created = OKRObjectiveRecord(
        id=OBJECTIVE_ID,
        tenant_id=TENANT_ID,
        title="Ship",
        owner_type="user",
        owner_id=USER_ID,
        period_start=datetime(2026, 1, 1, tzinfo=UTC).date(),
        period_end=datetime(2026, 3, 31, tzinfo=UTC).date(),
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    monkeypatch.setattr(okr_api.user_dao, "exists", AsyncMock(return_value=False))
    monkeypatch.setattr(okr_api.org_member_dao, "get", AsyncMock(return_value=member))
    create = AsyncMock(return_value=created)
    monkeypatch.setattr(okr_api.okr_objective_dao, "create", create)

    result = await okr_api.create_objective(
        okr_api.ObjectiveCreate(
            title="Ship",
            owner_type="user",
            owner_id=str(MEMBER_ID),
            period_start="2026-01-01",
            period_end="2026-03-31",
        ),
        user=_admin(),
    )

    assert result.owner_id == str(USER_ID)
    assert create.call_args.kwargs["obj_in"]["owner_id"] == USER_ID


@pytest.mark.asyncio
async def test_kr_progress_auto_status_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    kr = OKRKeyResultRecord(
        id=KR_ID,
        objective_id=OBJECTIVE_ID,
        title="Users",
        current_value=10.0,
        target_value=100.0,
        status="behind",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_updated_at=None,
    )
    updated = OKRKeyResultRecord(
        id=KR_ID,
        objective_id=OBJECTIVE_ID,
        title="Users",
        current_value=75.0,
        target_value=100.0,
        status="on_track",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_updated_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(okr_api.okr_key_result_dao, "get_with_tenant", AsyncMock(return_value=(kr, None)))
    update = AsyncMock(return_value=updated)
    monkeypatch.setattr(okr_api.okr_key_result_dao, "update", update)
    monkeypatch.setattr(okr_api.okr_progress_log_dao, "create", AsyncMock())

    result = await okr_api.update_kr_progress_endpoint(
        KR_ID,
        okr_api.ProgressUpdate(value=75.0),
        user=_admin(),
    )

    assert result.status == "on_track"
    assert update.call_args.kwargs["obj_in"]["status"] == "on_track"
