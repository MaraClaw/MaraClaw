from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.api import schedules
from app.dao.schedule_dao import AgentScheduleDAO, agent_schedule_dao
from test_scheduler_api import _HTTP, api, clock, owner, pool

__all__ = ["api", "clock", "owner", "pool"]


@pytest.mark.parametrize("preference", [None, False, True])
async def test_create_roundtrips_failure_preference_when_supplied_or_omitted(api, monkeypatch, preference):
    # Given an optional preference on a new schedule.
    enabled = preference is True
    create = AsyncMock(return_value=replace(api.row, notify_on_failure=enabled))
    monkeypatch.setattr(agent_schedule_dao, "create", create)
    payload = {"name": "Daily", "cron_expr": "0 12 * * *"}
    if preference is not None:
        payload["notify_on_failure"] = preference
    # When creating through HTTP.
    response = await api.client.post(api.url.rsplit("/", 1)[0] + "/", json=payload)
    # Then the public boolean and persisted preference agree, defaulting off.
    assert response.status_code == 201
    assert response.json()["notify_on_failure"] is enabled
    assert create.await_args_list[0].kwargs["obj_in"]["notify_on_failure"] is enabled


@pytest.mark.parametrize("preference", [None, False, True])
async def test_patch_preserves_or_changes_failure_preference_when_omitted_or_explicit(
    api, pool, monkeypatch, preference
):
    # Given the opposite stored preference so an explicit toggle must take effect.
    row = replace(api.row, notify_on_failure=preference is not True)
    pool.rows[row.id] = row
    api.get_schedule.return_value = row
    monkeypatch.setattr(agent_schedule_dao, "update", AgentScheduleDAO.update.__get__(agent_schedule_dao))
    payload = {} if preference is None else {"notify_on_failure": preference}
    # When patching through the HTTP and real DAO boundaries.
    response = await api.client.patch(api.url, json=payload)
    # Then omission preserves true and explicit false disables.
    expected = True if preference is None else preference
    assert response.status_code == 200
    assert response.json()["notify_on_failure"] is expected
    assert pool.rows[row.id].notify_on_failure is expected


@pytest.mark.parametrize("method", ["post", "patch"])
async def test_failure_preference_rejects_null_when_explicit(
    api: _HTTP, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    # Given a null instead of a boolean preference.
    mutation = AsyncMock()
    monkeypatch.setattr(agent_schedule_dao, "create" if method == "post" else "update", mutation)
    payload = {"name": "Daily", "cron_expr": "0 12 * * *", "notify_on_failure": None}
    url = api.url.rsplit("/", 1)[0] + "/" if method == "post" else api.url
    # When submitting it through HTTP.
    response = await api.client.request(method, url, json=payload)
    # Then validation rejects it before a schedule mutation.
    assert response.status_code == 422
    mutation.assert_not_awaited()


async def test_list_returns_boolean_without_pending_snapshot_when_preference_is_default(api, monkeypatch):
    # Given a schedule whose creator has not opted in.
    monkeypatch.setattr(agent_schedule_dao, "list_for_agent", AsyncMock(return_value=[api.row]))
    monkeypatch.setattr(schedules.user_dao, "labels_for_ids", AsyncMock(return_value={}))
    # When reading the public schedule list.
    response = await api.client.get(api.url.rsplit("/", 1)[0] + "/")
    # Then the default is explicit and internal occurrence fields stay private.
    assert response.status_code == 200
    (body,) = response.json()
    assert body["notify_on_failure"] is False
    assert "pending_notify_on_failure" not in body
