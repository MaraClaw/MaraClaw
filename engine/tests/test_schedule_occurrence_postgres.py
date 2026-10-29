import asyncio
import os
from collections.abc import AsyncIterator
from contextvars import Context
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import UUID, uuid4

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
                "VALUES (%(id)s, 'Schedule agent', 'QA', %(creator)s, 10, 5, 10, 'private', 'use', 30)",
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
                    "ends_at": row.ends_at,
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


@pytest.mark.parametrize("notify", [False, True])
async def test_live_reclaim_preserves_edits_and_counts_once(live_schedule: AgentScheduleRecord, notify: bool) -> None:
    _ = await agent_schedule_dao.update(db_obj=live_schedule, obj_in={"notify_on_failure": notify})
    (first,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    assert first.lease_token is not None
    assert await agent_schedule_dao.start_occurrence(first.id, first.lease_token, NOW)
    edited = await agent_schedule_dao.update(
        db_obj=live_schedule,
        obj_in={
            "instruction": "Future",
            "cron_expr": "invalid",
            "timezone": "Not/A_Zone",
            "notify_on_failure": not notify,
        },
    )
    compute = Mock(side_effect=AssertionError("Reclaims must not recompute"))
    (reclaimed,) = await agent_schedule_dao.claim_due(NOW + timedelta(minutes=5), compute)
    assert reclaimed.lease_token is not None
    assert reclaimed.lease_token != first.lease_token
    assert reclaimed == replace(edited, lease_token=reclaimed.lease_token, lease_expires_at=NOW + timedelta(minutes=10))
    assert reclaimed.pending_instruction == live_schedule.instruction
    assert first.pending_notify_on_failure is notify
    assert reclaimed.pending_notify_on_failure is notify
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


@pytest.mark.parametrize("field", [column for column in SLOT_COLUMNS if column != "pending_started_at"])
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


@pytest.mark.parametrize("offset_us", [-1, 0, 1])
async def test_live_cutoff_compares_due_not_worker_time(live_schedule: AgentScheduleRecord, offset_us: int) -> None:
    # Given an overdue occurrence one microsecond before, exactly at, or after the cutoff.
    assert live_schedule.next_run_at is not None
    cutoff = live_schedule.next_run_at - timedelta(microseconds=offset_us)
    await agent_schedule_dao.update(db_obj=live_schedule, obj_in={"ends_at": cutoff})
    # When a late worker tries admission after the cutoff has elapsed.
    claims = await agent_schedule_dao.claim_due(NOW, _next_run)
    # Then only the strict-before occurrence is admitted, with no ineligible successor.
    assert len(claims) == int(offset_us < 0)
    saved = await agent_schedule_dao.get(live_schedule.id)
    assert saved is not None
    assert saved.is_enabled is True
    assert saved.ends_at == cutoff
    if claims:
        assert claims[0].pending_due_at == live_schedule.next_run_at
        assert claims[0].next_run_at is None
        assert claims[0].lease_token is not None
        assert await agent_schedule_dao.finish_occurrence(saved.id, claims[0].lease_token, NOW)
        assert await agent_schedule_dao.claim_due(NEXT, _next_run) == []
    else:
        assert saved.pending_due_at is None
        assert saved.lease_token is None


@pytest.mark.parametrize("successor_offset_us", [-1, 0, 1])
async def test_live_final_eligible_claim_preserves_lease_without_successor(
    live_schedule: AgentScheduleRecord, successor_offset_us: int
) -> None:
    # Given an eligible occurrence and a successor near the cutoff.
    cutoff = NEXT - timedelta(microseconds=successor_offset_us)
    await agent_schedule_dao.update(db_obj=live_schedule, obj_in={"ends_at": cutoff})
    # When independent workers compete for the final eligible occurrence.
    async with connection_ctx():
        (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
        competitor = asyncio.create_task(agent_schedule_dao.claim_due(NOW, _next_run), context=Context())
        assert await asyncio.wait_for(competitor, timeout=2) == []
    # Then admission survives even when advancing reaches the cutoff; ownership remains usable.
    assert claim.is_enabled is True
    assert claim.next_run_at == (NEXT if successor_offset_us < 0 else None)
    assert claim.lease_token is not None
    assert await agent_schedule_dao.start_occurrence(claim.id, claim.lease_token, NOW)
    assert await agent_schedule_dao.finish_occurrence(claim.id, claim.lease_token, NOW)


@pytest.mark.parametrize("started", [False, True])
async def test_live_cutoff_edit_keeps_recovery_and_token_fencing(
    live_schedule: AgentScheduleRecord, started: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a claimed occurrence and a cutoff shortened to exclude its scheduled time.
    (first,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    assert first.lease_token is not None
    if started:
        assert await agent_schedule_dao.start_occurrence(first.id, first.lease_token, NOW)
    cutoff = NOW - timedelta(days=6)
    monkeypatch.setattr(schedules, "compute_next_run", lambda *_args, **_kwargs: NEXT)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=schedule_app(first)), base_url="http://qa"
    ) as client:
        response = await client.patch(
            f"/agents/{first.agent_id}/schedules/{first.id}", json={"ends_at": cutoff.isoformat()}
        )
    assert response.status_code == 200
    edited = await agent_schedule_dao.get(first.id)
    assert edited is not None
    assert edited.lease_token == first.lease_token
    assert edited.pending_due_at == first.pending_due_at
    assert edited.next_run_at is None
    assert await agent_schedule_dao.renew_occurrence(first.id, first.lease_token, NOW)
    compute = Mock(side_effect=AssertionError("Recovery must not recompute recurrence"))
    # When a restarted DAO recovers after lease expiry and cutoff.
    restarted = type(agent_schedule_dao)()
    (recovered,) = await restarted.claim_due(NOW + timedelta(minutes=5), compute)
    # Then only the rotated token can start and settle, with one start count and intact snapshot.
    assert recovered.lease_token is not None
    assert recovered.lease_token != first.lease_token
    assert recovered.pending_instruction == first.pending_instruction
    assert recovered.pending_notify_on_failure == first.pending_notify_on_failure
    for write in (restarted.renew_occurrence, restarted.start_occurrence, restarted.finish_occurrence):
        assert not await write(first.id, first.lease_token, NOW + timedelta(minutes=5))
    assert await restarted.start_occurrence(first.id, recovered.lease_token, NOW + timedelta(minutes=5))
    assert await restarted.finish_occurrence(first.id, recovered.lease_token, NOW + timedelta(minutes=5))
    saved = await restarted.get(first.id)
    assert saved is not None
    assert saved.run_count == live_schedule.run_count + 1
    assert saved.is_enabled is True
    assert saved.next_run_at is None
    compute.assert_not_called()


@pytest.mark.parametrize("new_cutoff", [None, "2026-10-18T19:00:00+07:00"])
async def test_live_http_cutoff_round_trip_and_restore(
    live_schedule: AgentScheduleRecord, monkeypatch: pytest.MonkeyPatch, new_cutoff: str | None
) -> None:
    # Given an initial cutoff before the next occurrence, persisted via the actual HTTP/SQL stack.
    monkeypatch.setattr(schedules, "compute_next_run", lambda *_args, **_kwargs: NEXT)
    base = f"/agents/{live_schedule.agent_id}/schedules/"
    cutoff = "2026-10-16T19:00:00+07:00"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=schedule_app(live_schedule)), base_url="http://qa"
    ) as client:
        created = await client.post(base, json={"name": "Bounded", "cron_expr": "0 12 * * *", "ends_at": cutoff})
        assert created.status_code == 201
        record = schedules.ScheduleOut.model_validate(created.json())
        assert record.ends_at == NEXT
        assert record.is_enabled is True
        assert record.next_run_at is None
        path = base + str(record.id)
        for payload in ({"name": "Renamed"}, {"timezone": "Asia/Ho_Chi_Minh"}):
            changed = await client.patch(path, json=payload)
            assert changed.status_code == 200
            assert schedules.ScheduleOut.model_validate(changed.json()).ends_at == NEXT
        # When explicitly removing or extending the cutoff of the exhausted schedule.
        restored = await client.patch(path, json={"ends_at": new_cutoff})
        listed = await client.get(base)
    # Then future scheduling is restored and list/persistence retain the same absolute instant.
    assert restored.status_code == 200
    assert listed.status_code == 200
    saved = schedules.ScheduleOut.model_validate(restored.json())
    assert saved.next_run_at == NEXT
    assert saved.is_enabled is True
    assert saved.ends_at == (datetime.fromisoformat(new_cutoff) if new_cutoff is not None else None)
    persisted = await agent_schedule_dao.get(saved.id)
    assert persisted is not None
    assert persisted.ends_at == saved.ends_at
    listed_record = schedules.ScheduleOut.model_validate(
        next(item for item in listed.json() if item["id"] == str(saved.id))
    )
    assert (listed_record.ends_at, listed_record.next_run_at, listed_record.timezone) == (
        saved.ends_at,
        NEXT,
        "Asia/Ho_Chi_Minh",
    )


@pytest.mark.parametrize("enabled", [False, True])
async def test_live_manual_run_remains_available_after_cutoff(
    manual_schedule: AgentScheduleRecord, manual_http: httpx.AsyncClient, enabled: bool
) -> None:
    # Given an exhausted schedule, either enabled or explicitly disabled.
    cutoff = NOW - timedelta(days=6)
    await agent_schedule_dao.update(db_obj=manual_schedule, obj_in={"ends_at": cutoff, "is_enabled": enabled})
    # When its creator explicitly requests a manual run over HTTP.
    response = await manual_http.post(f"/agents/{manual_schedule.agent_id}/schedules/{manual_schedule.id}/run")
    # Then the cutoff has no effect on manual acceptance or its instruction snapshot.
    assert response.status_code == 200
    assert response.json()["status"] == "triggered"
    receipt = await schedule_manual_run_dao.get(UUID(response.json()["run_id"]))
    assert receipt is not None
    assert receipt.instruction == manual_schedule.instruction
