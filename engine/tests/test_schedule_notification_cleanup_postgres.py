from uuid import uuid4

import httpx
import pytest

from app.dao.agent_dao import agent_dao
from app.dao.schedule_dao import agent_schedule_dao
from app.db.errors import CheckViolationError
from app.db.session import connection_ctx
from app.records.company_admission import AdmissionMethod
from app.records.company_transfer import CompanyTransferRecord, TransferChoice
from app.records.schedule import AgentScheduleRecord
from app.services.company_transfer_disposition import migrate_agent
from schedule_notification_postgres_fixtures import live_schedule, notifiable_schedule
from test_schedule_dao import NOW, _next_run
from test_schedule_occurrence_postgres import schedule_app

__all__ = ["live_schedule", "notifiable_schedule"]


async def test_live_company_transfer_clears_failure_snapshot_when_pending_automation_is_disabled(
    notifiable_schedule: AgentScheduleRecord,
) -> None:
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    agent = await agent_dao.get(claim.agent_id)
    assert agent is not None
    assert agent.tenant_id is not None
    target = uuid4()
    async with connection_ctx() as db:
        await db.execute(
            "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'Target', %(slug)s)",
            {"id": target, "slug": target.hex},
        )
    transfer = CompanyTransferRecord(
        id=uuid4(),
        applicant_user_id=claim.created_by,
        source_tenant_id=agent.tenant_id,
        target_tenant_id=target,
        choice=TransferChoice.PAID,
        method=AdmissionMethod.INVITATION,
        fee_amount_minor=0,
        currency="USD",
        instructions="",
        created_at=NOW,
    )

    await migrate_agent(agent, transfer)

    disabled = await agent_schedule_dao.get(claim.id)
    assert disabled is not None
    assert disabled.is_enabled is False
    assert disabled.pending_notify_on_failure is None
    assert disabled.pending_due_at is None
    assert disabled.pending_instruction is None
    assert disabled.lease_token is None
    assert disabled.lease_expires_at is None
    assert disabled.pending_started_at is None
    assert claim.lease_token is not None
    assert not await agent_schedule_dao.finish_occurrence(claim.id, claim.lease_token, NOW)


@pytest.mark.parametrize("orphaned", [False, True])
async def test_live_schema_rejects_failure_snapshot_when_no_occurrence_exists(
    live_schedule: AgentScheduleRecord, orphaned: bool
) -> None:
    assert live_schedule.notify_on_failure is False
    assert live_schedule.pending_notify_on_failure is None

    with pytest.raises(CheckViolationError):
        async with connection_ctx() as db:
            await db.execute(
                "UPDATE agent_schedules SET pending_notify_on_failure = %(snapshot)s WHERE id = %(id)s",
                {"snapshot": orphaned, "id": live_schedule.id},
            )


@pytest.mark.parametrize("preference", [None, False, True])
async def test_live_api_create_persists_boolean_when_failure_preference_is_omitted_or_supplied(
    notifiable_schedule: AgentScheduleRecord, preference: bool | None
) -> None:
    payload: dict[str, object] = {"name": "New", "cron_expr": "0 12 * * *"}
    if preference is not None:
        payload["notify_on_failure"] = preference
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=schedule_app(notifiable_schedule)), base_url="http://schedule-qa"
    ) as client:
        response = await client.post(f"/agents/{notifiable_schedule.agent_id}/schedules/", json=payload)

    assert response.status_code == 201
    expected = preference is True
    assert response.json()["notify_on_failure"] is expected
    async with connection_ctx() as db:
        assert (
            await db.fetchval(
                "SELECT notify_on_failure FROM agent_schedules WHERE id = %(id)s", {"id": response.json()["id"]}
            )
            is expected
        )
