from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.core import access_cache
from app.core.notification_kinds import NotificationKind
from app.dao.activity_log_dao import agent_activity_log_dao
from app.dao.notification_dao import notification_dao
from app.dao.schedule_dao import AgentScheduleDAO, agent_schedule_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.schedule import AgentScheduleRecord
from app.services import scheduler
from app.services.llm.background_guard import BackgroundCallOutcome
from schedule_notification_postgres_fixtures import live_schedule, notifiable_schedule
from test_schedule_dao import NOW, _next_run
from test_schedule_occurrence_postgres import schedule_app

__all__ = ["live_schedule", "notifiable_schedule"]


@pytest.mark.parametrize("snapshot", [False, True])
async def test_live_alert_commits_once_with_activity_when_snapshotted_failure_finishes(
    notifiable_schedule: AgentScheduleRecord, snapshot: bool
) -> None:
    _ = await agent_schedule_dao.update(db_obj=notifiable_schedule, obj_in={"notify_on_failure": snapshot})
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    _ = await agent_schedule_dao.update(db_obj=claim, obj_in={"notify_on_failure": not snapshot})

    await scheduler._execute_periodic_schedule(claim)
    await scheduler._execute_periodic_schedule(claim)

    alerts = await notification_dao.list_for_user(claim.created_by)
    history = await agent_activity_log_dao.list_schedule_history(claim.agent_id, claim.id)
    finished = await agent_schedule_dao.get(claim.id)
    assert finished is not None
    assert finished.pending_notify_on_failure is None
    assert len(history) == 1
    assert len(alerts) == int(snapshot)
    if snapshot:
        (alert,) = alerts
        assert alert.type is NotificationKind.SYSTEM_ALERT
        assert alert.user_id == claim.created_by
        assert alert.agent_id is None
        assert alert.ref_id == claim.id
        assert alert.link == f"/app/agents/{claim.agent_id}/schedules"
        assert not any(secret in str(alert) for secret in ("PRIVATE_REPLY", "PRIVATE_NAME", "PRIVATE_INSTRUCTION"))


@pytest.mark.parametrize(
    "change",
    [
        "member_inactive",
        "identity_inactive",
        "missing_identity",
        "password_change",
        "revoked",
        "cross_tenant",
        "ownership_transfer",
    ],
)
async def test_live_completion_reloads_original_creator_when_eligibility_changes_during_run(
    notifiable_schedule: AgentScheduleRecord, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    cached = AsyncMock(return_value="manage")
    monkeypatch.setattr(access_cache, "get_cached_level", cached)
    fresh_principal = AsyncMock(wraps=user_dao.get_with_identity)
    monkeypatch.setattr(user_dao, "get_with_identity", fresh_principal)
    new_owner = uuid4()

    async def outcome(**_kwargs: object) -> BackgroundCallOutcome:
        async with connection_ctx() as db:
            match change:
                case "member_inactive":
                    await db.execute("UPDATE users SET is_active = FALSE WHERE id = %(id)s", {"id": claim.created_by})
                case "identity_inactive" | "password_change":
                    await db.execute(
                        "UPDATE identities SET is_active = %(active)s, must_change_password = %(password)s "
                        "WHERE id = (SELECT identity_id FROM users WHERE id = %(id)s)",
                        {
                            "id": claim.created_by,
                            "active": change != "identity_inactive",
                            "password": change == "password_change",
                        },
                    )
                case "missing_identity":
                    await db.execute("UPDATE users SET identity_id = NULL WHERE id = %(id)s", {"id": claim.created_by})
                case "cross_tenant":
                    tenant = uuid4()
                    await db.execute(
                        "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'Other', %(slug)s)",
                        {"id": tenant, "slug": tenant.hex},
                    )
                    await db.execute(
                        "UPDATE users SET tenant_id = %(tenant)s WHERE id = %(id)s",
                        {"id": claim.created_by, "tenant": tenant},
                    )
                case "revoked" | "ownership_transfer":
                    await db.execute(
                        "INSERT INTO users (id, display_name, tenant_id) "
                        "SELECT %(owner)s, 'New owner', tenant_id FROM users WHERE id = %(id)s",
                        {"owner": new_owner, "id": claim.created_by},
                    )
                    await db.execute(
                        "UPDATE agents SET creator_id = %(owner)s, access_mode = %(mode)s, "
                        "company_access_level = 'use' WHERE id = %(id)s",
                        {
                            "id": claim.agent_id,
                            "owner": new_owner,
                            "mode": "company" if change == "ownership_transfer" else "private",
                        },
                    )
                case _:
                    raise AssertionError(change)
        return BackgroundCallOutcome("failure", "PRIVATE_REPLY")

    monkeypatch.setattr("app.services.llm.call_agent_llm_with_tools", outcome)

    await scheduler._execute_periodic_schedule(claim)

    alerts = await notification_dao.list_for_user(claim.created_by)
    assert len(alerts) == int(change == "ownership_transfer")
    assert await notification_dao.list_for_user(new_owner) == []
    assert len(await agent_activity_log_dao.list_schedule_history(claim.agent_id, claim.id)) == 1
    assert fresh_principal.await_args_list[0].kwargs == {"fresh": True}
    cached.assert_not_awaited()


async def test_live_notification_insert_failure_rolls_back_finalization_and_activity_then_reclaims(
    notifiable_schedule: AgentScheduleRecord,
) -> None:
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    async with connection_ctx() as db:
        await db.execute(
            "CREATE FUNCTION reject_schedule_alert() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'notification unavailable'; END $$"
        )
        await db.execute(
            "CREATE TRIGGER reject_schedule_alert BEFORE INSERT ON notifications "
            "FOR EACH ROW EXECUTE FUNCTION reject_schedule_alert()"
        )

    await scheduler._execute_periodic_schedule(claim)

    interrupted = await agent_schedule_dao.get(claim.id)
    assert interrupted is not None
    assert interrupted.pending_due_at == claim.pending_due_at
    assert interrupted.pending_notify_on_failure is True
    assert interrupted.pending_started_at == NOW
    assert interrupted.run_count == claim.run_count + 1
    assert await agent_activity_log_dao.list_schedule_history(claim.agent_id, claim.id) == []
    assert await notification_dao.list_for_user(claim.created_by) == []
    async with connection_ctx() as db:
        await db.execute("DROP TRIGGER reject_schedule_alert ON notifications")
        await db.execute(
            "UPDATE agent_schedules SET lease_expires_at = %(expired)s, notify_on_failure = FALSE WHERE id = %(id)s",
            {"id": claim.id, "expired": NOW - timedelta(seconds=1)},
        )
    (reclaimed,) = await AgentScheduleDAO().claim_due(NOW, _next_run)
    assert reclaimed.pending_notify_on_failure is True
    await scheduler._execute_periodic_schedule(reclaimed)
    assert len(await notification_dao.list_for_user(claim.created_by)) == 1
    assert len(await agent_activity_log_dao.list_schedule_history(claim.agent_id, claim.id)) == 1


@pytest.mark.parametrize("preference", [False, True])
async def test_live_api_roundtrips_preference_and_null_rejection(
    notifiable_schedule: AgentScheduleRecord, preference: bool
) -> None:
    app = schedule_app(notifiable_schedule)
    path = f"/agents/{notifiable_schedule.agent_id}/schedules/{notifiable_schedule.id}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://schedule-qa") as client:
        patched = await client.patch(path, json={"notify_on_failure": preference})
        omitted = await client.patch(path, json={"name": "Renamed"})
        rejected = await client.patch(path, json={"notify_on_failure": None})
        listed = await client.get(path.rsplit("/", 1)[0] + "/")
    assert patched.status_code == omitted.status_code == listed.status_code == 200
    assert patched.json()["notify_on_failure"] is preference
    assert omitted.json()["notify_on_failure"] is preference
    assert listed.json()[0]["notify_on_failure"] is preference
    assert rejected.status_code == 422
    persisted = await agent_schedule_dao.get(notifiable_schedule.id)
    assert persisted is not None
    assert persisted.notify_on_failure is preference
