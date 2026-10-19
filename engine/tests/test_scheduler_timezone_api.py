from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.api import schedules
from test_schedule_dao import pool as pool
from test_scheduler import clock as clock
from test_scheduler_api import _HTTP, api as api
from test_scheduler_tasks import owner as owner


@pytest.mark.parametrize("zone", [None, "Asia/Ho_Chi_Minh"])
async def test_create_persists_default_or_selected_zone(
    api: _HTTP, monkeypatch: pytest.MonkeyPatch, zone: str | None
) -> None:
    create = AsyncMock(return_value=replace(api.row, timezone=zone or "UTC"))
    monkeypatch.setattr(schedules.agent_schedule_dao, "create", create)
    payload = {"name": "Morning", "cron_expr": "0 9 * * *"}
    if zone is not None:
        payload["timezone"] = zone
    response = await api.client.post(api.url.rsplit("/", 1)[0] + "/", json=payload)
    assert response.status_code == 201
    values = create.await_args_list[0].kwargs["obj_in"]
    assert values["timezone"] == (zone or "UTC")
    assert values["next_run_at"] == datetime(2026, 10, 16, 2 if zone is not None else 9, tzinfo=UTC)
    assert response.json()["timezone"] == (zone or "UTC")


@pytest.mark.parametrize("enabled", [False, True])
async def test_zone_only_patch_recomputes_stored_cron(api: _HTTP, enabled: bool) -> None:
    api.get_schedule.return_value = replace(api.row, is_enabled=enabled)
    response = await api.client.patch(api.url, json={"timezone": "Asia/Ho_Chi_Minh"})
    assert response.status_code == 200
    assert api.update.await_args_list[0].kwargs["obj_in"] == {
        "timezone": "Asia/Ho_Chi_Minh",
        "next_run_at": datetime(2026, 10, 16, 5, tzinfo=UTC) if enabled else None,
    }


@pytest.mark.parametrize("payload", [{"name": "Renamed"}, {"cron_expr": "0 9 * * *"}, {"is_enabled": True}])
async def test_patch_omission_preserves_saved_zone(api: _HTTP, payload: dict[str, object]) -> None:
    api.get_schedule.return_value = replace(api.row, timezone="Asia/Ho_Chi_Minh")
    response = await api.client.patch(api.url, json=payload)
    assert response.status_code == 200
    values = api.update.await_args_list[0].kwargs["obj_in"]
    assert "timezone" not in values
    if "name" in payload:
        assert "next_run_at" not in values
    else:
        assert values["next_run_at"] == datetime(2026, 10, 16, 2 if "cron_expr" in payload else 5, tzinfo=UTC)


@pytest.mark.parametrize("zone", ["Not/A_Zone", "", None])
@pytest.mark.parametrize("method", ["post", "patch"])
async def test_invalid_zone_is_rejected_before_write(
    api: _HTTP, monkeypatch: pytest.MonkeyPatch, zone: str | None, method: str
) -> None:
    create = AsyncMock(return_value=api.row)
    monkeypatch.setattr(schedules.agent_schedule_dao, "create", create)
    payload: dict[str, object] = {"timezone": zone, "is_enabled": False}
    if method == "post":
        payload.update(name="Daily", cron_expr="0 12 * * *")
    response = await api.client.request(
        method, api.url.rsplit("/", 1)[0] + "/" if method == "post" else api.url, json=payload
    )
    assert response.status_code in (400, 422)
    assert "timezone" in str(response.json()).lower()
    create.assert_not_awaited()
    api.update.assert_not_awaited()


@pytest.mark.parametrize("zone", [None, "UTC"])
async def test_unavailable_timezone_data_rejects_create_without_host_fallback(
    api: _HTTP, monkeypatch: pytest.MonkeyPatch, zone: str | None
) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    def unavailable(_key: str) -> ZoneInfo:
        raise ZoneInfoNotFoundError("Timezone database unavailable")

    create = AsyncMock()
    monkeypatch.setattr(schedules.agent_schedule_dao, "create", create)
    monkeypatch.setattr("app.services.schedule_time.ZoneInfo", unavailable)
    payload = {"name": "Daily", "cron_expr": "0 12 * * *"}
    if zone is not None:
        payload["timezone"] = zone
    response = await api.client.post(api.url.rsplit("/", 1)[0] + "/", json=payload)
    assert response.status_code in (400, 422)
    assert "timezone data unavailable" in response.text
    create.assert_not_awaited()
