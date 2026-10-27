from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core import access_cache
from app.dao.schedule_dao import agent_schedule_dao
from app.db.session import connection_ctx
from app.records.schedule import AgentScheduleRecord
from app.services import schedule_lease, scheduler
from app.services.llm.background_guard import BackgroundCallOutcome
from test_schedule_occurrence_postgres import live_schedule
from test_scheduler import _Clock

__all__ = ["live_schedule"]


@pytest.fixture
async def notifiable_schedule(
    live_schedule: AgentScheduleRecord, monkeypatch: pytest.MonkeyPatch
) -> AgentScheduleRecord:
    identity, tenant = uuid4(), uuid4()
    async with connection_ctx() as db:
        await db.execute("INSERT INTO identities (id) VALUES (%(id)s)", {"id": identity})
        await db.execute(
            "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'Notify QA', %(slug)s)",
            {"id": tenant, "slug": tenant.hex},
        )
        await db.execute(
            "UPDATE users SET identity_id = %(identity)s, tenant_id = %(tenant)s WHERE id = %(id)s",
            {"identity": identity, "tenant": tenant, "id": live_schedule.created_by},
        )
        await db.execute(
            "UPDATE agents SET tenant_id = %(tenant)s, status = 'running', company_access_level = 'use' WHERE id = %(id)s",
            {"tenant": tenant, "id": live_schedule.agent_id},
        )
    monkeypatch.setattr(scheduler, "datetime", _Clock)
    monkeypatch.setattr(schedule_lease, "datetime", _Clock)
    monkeypatch.setattr(access_cache, "read_acl_version", AsyncMock(return_value="0"))
    monkeypatch.setattr("app.services.agent_context.build_agent_context", AsyncMock(return_value=("static", "dynamic")))
    monkeypatch.setattr(
        "app.services.llm.call_agent_llm_with_tools",
        AsyncMock(return_value=BackgroundCallOutcome("failure", "PRIVATE_REPLY")),
    )
    return await agent_schedule_dao.update(
        db_obj=live_schedule,
        obj_in={"notify_on_failure": True, "name": "PRIVATE_NAME", "instruction": "PRIVATE_INSTRUCTION"},
    )
