import asyncio
import os
from collections.abc import AsyncIterator
from contextvars import Context
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.api import schedules
from app.core.security import get_current_user, load_user_principal
from app.dao.schedule_dao import agent_schedule_dao
from app.dao.schedule_manual_run_dao import schedule_manual_run_dao
from app.db import session as session_module
from app.db.connection import DbConnection
from app.db.errors import CheckViolationError
from app.db.pool import close_pool, init_pool
from app.db.session import bind_crud_connection, connection_ctx
from app.records.schedule import AgentScheduleRecord
from app.records.schedule_manual_run import ScheduleManualRunRecord
from app.records.user import UserRecord
from app.scripts.bootstrap_db import SCHEMA_BASELINE, _statement_bodies
from schedule_occurrence_persistence_fakes import SLOT_COLUMNS
from test_schedule_dao import NEXT, NOW, _next_run, _schedule


@pytest.fixture
async def live_schedule() -> AsyncIterator[AgentScheduleRecord]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for isolated PostgreSQL QA")
    schema = f"schedule_occurrence_qa_{uuid4().hex}"
    context_token = session_module._conn_ctx.set(None)
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            for _ in range(2):
                for statement in _statement_bodies(SCHEMA_BASELINE.read_text()):
                    await db.execute(statement)
            row = _schedule()
            await db.execute(
                "INSERT INTO users (id, display_name) VALUES (%(id)s, 'Schedule owner')", {"id": row.created_by}
            )
            await db.execute(
                "INSERT INTO agents (id, name, role_description, creator_id, max_triggers, min_poll_interval_min, "
                "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
                "VALUES (%(id)s, 'Schedule agent', 'QA', %(creator)s, 10, 5, 10, 'private', 'none', 30)",
                {"id": row.agent_id, "creator": row.created_by},
            )
            await init_pool(conninfo=make_conninfo(conninfo, options=f"-csearch_path={schema}"), min_size=1, max_size=4)
            yield await agent_schedule_dao.create(
                obj_in={
                    "id": row.id,
                    "agent_id": row.agent_id,
                    "name": row.name,
                    "instruction": row.instruction,
                    "cron_expr": row.cron_expr,
                    "timezone": row.timezone,
                    "created_by": row.created_by,
                    "next_run_at": row.next_run_at,
                    "run_count": row.run_count,
                }
            )
        finally:
            await close_pool()
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')
            session_module._conn_ctx.reset(context_token)


@pytest.fixture
async def manual_schedule(live_schedule: AgentScheduleRecord) -> AgentScheduleRecord:
    async with connection_ctx() as db:
        await db.execute("UPDATE agents SET status = 'running' WHERE id = %(id)s", {"id": live_schedule.agent_id})
    return live_schedule


def schedule_app(schedule: AgentScheduleRecord) -> FastAPI:
    app = FastAPI()
    app.include_router(schedules.router, dependencies=[Depends(bind_crud_connection)])

    async def user() -> UserRecord:
        return await load_user_principal(schedule.created_by, fresh=True)

    app.dependency_overrides[get_current_user] = user
    return app


@pytest.fixture
async def manual_http(manual_schedule: AgentScheduleRecord) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=schedule_app(manual_schedule)), base_url="http://schedule-qa"
    ) as client:
        yield client


async def accept_claim(schedule: AgentScheduleRecord) -> ScheduleManualRunRecord:
    accepted = await schedule_manual_run_dao.accept(schedule.id, schedule.created_by, None)
    assert accepted is not None
    (claim,) = await schedule_manual_run_dao.claim_pending(datetime.now(UTC))
    return claim


async def test_live_competing_claim_skips_locked_row(live_schedule: AgentScheduleRecord) -> None:
    async with connection_ctx():
        (first,) = await agent_schedule_dao.claim_due(NOW, _next_run)
        competitor = asyncio.create_task(agent_schedule_dao.claim_due(NOW, _next_run), context=Context())
        assert await asyncio.wait_for(competitor, timeout=2) == []
    assert await agent_schedule_dao.get(first.id) == first
    assert first.pending_due_at == live_schedule.next_run_at
    assert first.pending_instruction == live_schedule.instruction
    assert first.next_run_at == NEXT
    assert first.lease_expires_at == NOW + timedelta(minutes=5)


async def test_live_reclaim_preserves_edits_and_counts_once(live_schedule: AgentScheduleRecord) -> None:
    (first,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    assert first.lease_token is not None
    assert await agent_schedule_dao.start_occurrence(first.id, first.lease_token, NOW)
    edited = await agent_schedule_dao.update(
        db_obj=live_schedule, obj_in={"instruction": "Future", "cron_expr": "invalid", "timezone": "Not/A_Zone"}
    )
    compute = Mock(side_effect=AssertionError("Reclaims must not recompute"))
    (reclaimed,) = await agent_schedule_dao.claim_due(NOW + timedelta(minutes=5), compute)
    assert reclaimed.lease_token is not None
    assert reclaimed.lease_token != first.lease_token
    assert reclaimed == replace(edited, lease_token=reclaimed.lease_token, lease_expires_at=NOW + timedelta(minutes=10))
    assert reclaimed.pending_instruction == live_schedule.instruction
    assert reclaimed.pending_due_at == live_schedule.next_run_at
    outcomes = await asyncio.gather(
        *(
            agent_schedule_dao.start_occurrence(reclaimed.id, reclaimed.lease_token, NOW + timedelta(minutes=6))
            for _ in range(2)
        )
    )
    assert outcomes == [True, True]
    assert await agent_schedule_dao.get(reclaimed.id) == reclaimed
    compute.assert_not_called()


async def test_live_competing_first_starts_increment_once(live_schedule: AgentScheduleRecord) -> None:
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    assert claim.lease_token is not None
    outcomes = await asyncio.gather(
        *(agent_schedule_dao.start_occurrence(claim.id, claim.lease_token, NOW) for _ in range(2))
    )
    assert outcomes == [True, True]
    assert await agent_schedule_dao.get(claim.id) == replace(
        claim, run_count=live_schedule.run_count + 1, last_run_at=NOW, pending_started_at=NOW
    )


@pytest.mark.parametrize("operation", ["renew_occurrence", "start_occurrence", "finish_occurrence"])
async def test_live_owner_write_rejects_exact_expiry_and_rotated_token(
    live_schedule: AgentScheduleRecord, operation: str
) -> None:
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    assert claim.lease_token is not None
    write = {
        "renew_occurrence": agent_schedule_dao.renew_occurrence,
        "start_occurrence": agent_schedule_dao.start_occurrence,
        "finish_occurrence": agent_schedule_dao.finish_occurrence,
    }[operation]
    expired_at = NOW + timedelta(minutes=5)
    assert not await write(claim.id, claim.lease_token, expired_at)
    (reclaimed,) = await agent_schedule_dao.claim_due(expired_at, _next_run)
    assert not await write(claim.id, claim.lease_token, expired_at)
    assert await agent_schedule_dao.get(claim.id) == reclaimed


async def test_live_renew_finish_and_disable_fence_old_owner(live_schedule: AgentScheduleRecord) -> None:
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    assert claim.lease_token is not None
    now = NOW + timedelta(minutes=4)
    assert await agent_schedule_dao.renew_occurrence(claim.id, claim.lease_token, now)
    assert await agent_schedule_dao.claim_due(NOW + timedelta(minutes=5), _next_run) == []
    assert await agent_schedule_dao.finish_occurrence(claim.id, claim.lease_token, NOW + timedelta(minutes=8))
    finished = await agent_schedule_dao.get(claim.id)
    assert finished == replace(live_schedule, next_run_at=NEXT)
    disabled = await agent_schedule_dao.update(db_obj=live_schedule, obj_in={"is_enabled": False})
    assert disabled == replace(live_schedule, is_enabled=False, next_run_at=None)
    assert not await agent_schedule_dao.start_occurrence(claim.id, claim.lease_token, now)


async def test_live_claim_rollback_restores_slot_and_future_cron(live_schedule: AgentScheduleRecord) -> None:
    async def fail_transaction() -> None:
        async with connection_ctx():
            await agent_schedule_dao.claim_due(NOW, _next_run)
            raise RuntimeError("claim interrupted")

    with pytest.raises(RuntimeError, match="claim interrupted"):
        await fail_transaction()
    assert await agent_schedule_dao.get(live_schedule.id) == live_schedule
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    assert claim.pending_due_at == live_schedule.next_run_at


async def test_live_finalization_rollback_preserves_started_occurrence(live_schedule: AgentScheduleRecord) -> None:
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    assert claim.lease_token is not None
    assert await agent_schedule_dao.start_occurrence(claim.id, claim.lease_token, NOW)

    async def fail_transaction() -> None:
        assert claim.lease_token is not None
        async with connection_ctx():
            assert await agent_schedule_dao.finish_occurrence(claim.id, claim.lease_token, NOW)
            raise RuntimeError("activity failed")

    with pytest.raises(RuntimeError, match="activity failed"):
        await fail_transaction()
    assert await agent_schedule_dao.get(claim.id) == replace(
        claim, pending_started_at=NOW, last_run_at=NOW, run_count=live_schedule.run_count + 1
    )


@pytest.mark.parametrize("scope", ["tenant", "creator", "creator_tenant"])
async def test_live_bulk_disable_revokes_pending_lease(live_schedule: AgentScheduleRecord, scope: str) -> None:
    tenant_id = uuid4()
    async with connection_ctx() as db:
        await db.execute(
            "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'Schedule QA', %(slug)s)",
            {"id": tenant_id, "slug": tenant_id.hex},
        )
        await db.execute(
            "UPDATE agents SET tenant_id = %(tenant)s WHERE id = %(id)s",
            {"tenant": tenant_id, "id": live_schedule.agent_id},
        )
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    assert claim.lease_token is not None
    if scope == "tenant":
        count = await agent_schedule_dao.disable_for_tenant(tenant_id)
    else:
        count = await agent_schedule_dao.disable_for_creator(
            live_schedule.created_by, tenant_id=tenant_id if scope == "creator_tenant" else None
        )
    assert count == 1
    assert await agent_schedule_dao.get(claim.id) == replace(live_schedule, is_enabled=False, next_run_at=None)
    assert not await agent_schedule_dao.finish_occurrence(claim.id, claim.lease_token, NOW)


@pytest.mark.parametrize("field", SLOT_COLUMNS[:4])
async def test_live_schema_rejects_partial_occurrence(live_schedule: AgentScheduleRecord, field: str) -> None:
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    assert claim.lease_token is not None
    assert await agent_schedule_dao.start_occurrence(claim.id, claim.lease_token, NOW)
    with pytest.raises(CheckViolationError):
        async with connection_ctx() as db:
            await db.execute(
                sql.SQL("UPDATE agent_schedules SET {} = NULL WHERE id = %(id)s")
                .format(sql.Identifier(field))
                .as_string(),
                {"id": claim.id},
            )
