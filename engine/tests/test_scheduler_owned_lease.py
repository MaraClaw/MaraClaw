import asyncio
import time
from contextvars import ContextVar
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from app.core.logging import get_trace_id, set_trace_id
from app.dao.schedule_dao import agent_schedule_dao
from app.db import session
from app.services import schedule_lease
from app.services.llm.background_guard import ScheduleLeaseLostError
from app.services.schedule_lease import ScheduleLeaseHold
from test_schedule_dao import NOW, _next_run, _schedule, pool
from test_scheduler import _Clock
from test_scheduler_tasks import owner

__all__ = ["owner", "pool"]


@pytest.fixture(autouse=True)
def heartbeat_interval(monkeypatch):
    monkeypatch.setattr(schedule_lease, "LEASE_RENEW_INTERVAL", timedelta(milliseconds=1))


@pytest.fixture
async def occurrence(pool, monkeypatch):
    row = _schedule()
    pool.rows[row.id] = row
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    monkeypatch.setattr(schedule_lease, "datetime", _Clock)
    return claim


async def test_lease_renewals_use_fresh_contexts_and_join_owned_heartbeat(occurrence, pool, monkeypatch):
    probe = ContextVar("scheduler-parent-context", default="fresh")
    probe.set("parent")
    set_trace_id("periodic-trace")
    renewed = asyncio.Event()
    renew = agent_schedule_dao.renew_occurrence
    calls = 0

    async def isolated(*args):
        nonlocal calls
        assert session.get_connection() is None
        assert session._completion_ctx.get() is None
        assert probe.get() == "fresh"
        assert get_trace_id() == "periodic-trace"
        result = await renew(*args)
        calls += 1
        if calls > 1:
            renewed.set()
        return result

    monkeypatch.setattr(agent_schedule_dao, "renew_occurrence", isolated)
    baseline = set(asyncio.all_tasks())
    async with session.connection_ctx():
        parent = pool.connections[-1]
        async with ScheduleLeaseHold(occurrence.id, occurrence.lease_token) as hold:
            await asyncio.wait_for(renewed.wait(), 1)
            assert not parent.executed
            assert parent.commits == 0
            assert all(raw.commits == 1 for raw in pool.connections if raw is not parent)
        assert hold.heartbeat_task is not None
        assert hold.heartbeat_task.done()
    assert set(asyncio.all_tasks()) == baseline


async def test_heartbeat_revocation_cancels_owner_and_escapes_as_lease_loss(occurrence, pool):
    baseline = set(asyncio.all_tasks())

    async def run():
        async with ScheduleLeaseHold(occurrence.id, occurrence.lease_token):
            pool.rows[occurrence.id] = replace(pool.rows[occurrence.id], lease_token=uuid4())
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    with pytest.raises(ScheduleLeaseLostError):
        await asyncio.wait_for(task, 1)
    assert not task.cancelled()
    assert set(asyncio.all_tasks()) == baseline


@pytest.mark.parametrize("clock", ["monotonic", "wall"])
async def test_sync_admission_rejects_deadline_before_heartbeat(occurrence, monkeypatch, clock):
    async with ScheduleLeaseHold(occurrence.id, occurrence.lease_token) as hold:
        with monkeypatch.context() as patch:
            if clock == "monotonic":
                patch.setattr(hold.loop, "time", lambda: hold.deadline + 1)
            else:
                patch.setattr(time, "time", lambda: hold.wall_deadline + 1)
            with pytest.raises(ScheduleLeaseLostError):
                hold.ensure_held()
        assert hold.fired_reason is None


async def test_confirm_rejects_wall_expiry_during_renewal_without_extending_deadline(occurrence, monkeypatch):
    async with ScheduleLeaseHold(occurrence.id, occurrence.lease_token) as hold:
        previous = hold.deadline, hold.wall_deadline

        async def late(*_args):
            monkeypatch.setattr(time, "time", lambda: previous[1] + 1)
            return True

        monkeypatch.setattr(agent_schedule_dao, "renew_occurrence", late)
        with pytest.raises(ScheduleLeaseLostError):
            await hold.confirm()
        assert (hold.deadline, hold.wall_deadline) == previous


async def test_initial_confirmation_rejects_revoked_claim_before_body(occurrence, pool):
    pool.rows[occurrence.id] = replace(pool.rows[occurrence.id], lease_token=uuid4())
    baseline = set(asyncio.all_tasks())
    with pytest.raises(ScheduleLeaseLostError):
        async with ScheduleLeaseHold(occurrence.id, occurrence.lease_token):
            pytest.fail("revoked owner admitted")
    assert set(asyncio.all_tasks()) == baseline


async def test_transient_heartbeat_failure_retries_within_original_deadline(occurrence, monkeypatch):
    renewed = asyncio.Event()
    renew = agent_schedule_dao.renew_occurrence
    calls = 0

    async def transient(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("temporary connection failure")
        result = await renew(*args)
        if calls > 2:
            renewed.set()
        return result

    monkeypatch.setattr(agent_schedule_dao, "renew_occurrence", transient)
    async with ScheduleLeaseHold(occurrence.id, occurrence.lease_token) as hold:
        await asyncio.wait_for(renewed.wait(), 1)
        hold.ensure_held()
        assert hold.fired_reason is None


async def test_repeated_shutdown_cancellation_waits_for_heartbeat_cleanup(occurrence, monkeypatch):
    renewing, cleaning, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release = asyncio.Event()
    renew = agent_schedule_dao.renew_occurrence
    calls = 0

    async def blocked(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return await renew(*args)
        renewing.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    async def run():
        async with ScheduleLeaseHold(occurrence.id, occurrence.lease_token):
            await asyncio.Event().wait()

    monkeypatch.setattr(agent_schedule_dao, "renew_occurrence", blocked)
    task = asyncio.create_task(run())
    await asyncio.wait_for(renewing.wait(), 1)
    task.cancel()
    await asyncio.wait_for(cleaning.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert cleaned.is_set()


async def test_external_cancellation_wins_when_heartbeat_also_revokes(occurrence):
    async def run():
        async with ScheduleLeaseHold(occurrence.id, occurrence.lease_token) as hold:
            hold._fire("revoked")
            hold.owner_task.cancel()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert task.cancelled()
