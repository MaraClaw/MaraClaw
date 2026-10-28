from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.core import access_cache
from app.core.security import get_current_user, load_user_principal
from app.dao.activity_log_dao import agent_activity_log_dao
from app.dao.schedule_dao import agent_schedule_dao
from app.dao.schedule_manual_run_dao import schedule_manual_run_dao as runs
from app.db.session import connection_ctx
from app.records.schedule import AgentScheduleRecord
from app.records.user import UserRecord
from test_schedule_occurrence_postgres import (
    accept_claim,
    live_schedule,
    manual_schedule,
    schedule_app,
)

__all__ = ["live_schedule", "manual_schedule"]


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("actor", ["requester", "creator", "member", "org_admin", "platform_admin"])
async def test_live_receipts_require_requester_or_creator_even_for_admins(
    manual_schedule: AgentScheduleRecord, method: str, actor: str
) -> None:
    # Given shared agent access and a manual run owned by a non-creator requester.
    requester, outsider = uuid4(), uuid4()
    async with connection_ctx() as db:
        await db.execute("INSERT INTO users (id, display_name) VALUES (%(id)s, 'Requester')", {"id": requester})
        await db.execute(
            "INSERT INTO users (id, display_name, role) VALUES (%(id)s, 'Other', %(role)s)",
            {"id": outsider, "role": actor if actor in {"org_admin", "platform_admin"} else "member"},
        )
        await db.execute(
            "UPDATE agents SET access_mode = 'company', company_access_level = 'use' WHERE id = %(id)s",
            {"id": manual_schedule.agent_id},
        )
    run = await runs.accept(manual_schedule.id, requester, None)
    assert run is not None
    user_id = requester if actor == "requester" else manual_schedule.created_by if actor == "creator" else outsider
    app = schedule_app(manual_schedule)

    async def current_user() -> UserRecord:
        return await load_user_principal(user_id, fresh=True)

    app.dependency_overrides[get_current_user] = current_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qa") as client:
        path = f"/agents/{run.agent_id}/schedules/{run.schedule_id}/runs/{run.id}"
        # When an authorized agent user reads or cancels someone else's receipt.
        response = await client.request(method, path + ("/cancel" if method == "POST" else ""))
    # Then admins have no receipt-specific bypass, while either owner is allowed.
    assert response.status_code == (200 if actor in {"requester", "creator"} else 403)
    if response.status_code == 403:
        assert await runs.get(run.id) == run
        assert await agent_activity_log_dao.list_schedule_history(run.agent_id, run.schedule_id) == []


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("change", ["inactive", "access", "stopped", "expired_manager", "expired_user"])
async def test_live_manual_control_uses_fresh_access_and_existing_expiry_policy(
    manual_schedule: AgentScheduleRecord, monkeypatch: pytest.MonkeyPatch, method: str, change: str
) -> None:
    # Given a stale authenticated principal and an access-cache value that must not be trusted.
    run = await accept_claim(manual_schedule)
    stale_user = await load_user_principal(run.requester_id, fresh=True)
    app = schedule_app(manual_schedule)
    app.dependency_overrides[get_current_user] = lambda: stale_user
    cached = AsyncMock(return_value="manage")
    monkeypatch.setattr(access_cache, "get_cached_level", cached)
    async with connection_ctx() as db:
        match change:
            case "inactive":
                await db.execute("UPDATE users SET is_active = FALSE WHERE id = %(id)s", {"id": run.requester_id})
            case "access" | "expired_user":
                owner = uuid4()
                await db.execute("INSERT INTO users (id, display_name) VALUES (%(id)s, 'New owner')", {"id": owner})
                await db.execute(
                    "UPDATE agents SET creator_id = %(owner)s, access_mode = %(mode)s, "
                    "company_access_level = 'use', is_expired = %(expired)s WHERE id = %(id)s",
                    {
                        "owner": owner,
                        "mode": "company" if change == "expired_user" else "private",
                        "expired": change == "expired_user",
                        "id": run.agent_id,
                    },
                )
            case "stopped":
                await db.execute("UPDATE agents SET status = 'stopped' WHERE id = %(id)s", {"id": run.agent_id})
            case "expired_manager":
                await db.execute("UPDATE agents SET is_expired = TRUE WHERE id = %(id)s", {"id": run.agent_id})
            case _:
                raise AssertionError(change)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qa") as client:
        # When status or cancel freshly authorizes after the change.
        path = f"/agents/{run.agent_id}/schedules/{run.schedule_id}/runs/{run.id}"
        response = await client.request(method, path + ("/cancel" if method == "POST" else ""))
    # Then fresh policy rejects revoked/expired-use access without adding stopped/manager-expiry gates.
    expected = 200 if change in {"stopped", "expired_manager"} else 403
    assert response.status_code == expected
    cached.assert_not_awaited()
    if expected == 403:
        assert await runs.get(run.id) == run


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("binding", ["missing", "schedule", "agent", "deleted"])
async def test_live_manual_control_scopes_all_path_bindings(
    manual_schedule: AgentScheduleRecord, method: str, binding: str
) -> None:
    # Given an existing receipt and another schedule/agent owned by the same caller.
    run = await accept_claim(manual_schedule)
    agent_id, schedule_id, run_id = run.agent_id, run.schedule_id, run.id
    async with connection_ctx() as db:
        match binding:
            case "missing":
                run_id = uuid4()
            case "schedule":
                other = await agent_schedule_dao.create(
                    obj_in={
                        "agent_id": agent_id,
                        "name": "Other",
                        "instruction": "Other",
                        "cron_expr": "0 0 * * *",
                        "created_by": run.requester_id,
                    }
                )
                schedule_id = other.id
            case "agent":
                agent_id = uuid4()
                await db.execute(
                    "INSERT INTO agents (id, name, role_description, creator_id, max_triggers, min_poll_interval_min, "
                    "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
                    "VALUES (%(id)s, 'Other', 'QA', %(creator)s, 10, 5, 10, 'private', 'use', 30)",
                    {"id": agent_id, "creator": run.requester_id},
                )
            case "deleted":
                await agent_schedule_dao.delete(record_id=schedule_id)
            case _:
                raise AssertionError(binding)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=schedule_app(manual_schedule)), base_url="http://qa"
    ) as client:
        # When caller-controlled path identities mismatch or no longer exist.
        path = f"/agents/{agent_id}/schedules/{schedule_id}/runs/{run_id}"
        response = await client.request(method, path + ("/cancel" if method == "POST" else ""))
    # Then no receipt data or cancellation acknowledgement is exposed.
    assert response.status_code == 404
    assert await agent_activity_log_dao.list_schedule_history(run.agent_id, run.schedule_id) == []
