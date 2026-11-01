from dataclasses import replace
from datetime import timedelta

import anyio
import pytest
from psycopg.errors import LockNotAvailable
from psycopg.pq import TransactionStatus
from tests.channel_submission_fixtures import channel_db, receipt_db, whatsapp_relay_db

from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao as dao
from app.db.connection import DbConnection
from app.db.errors import LockNotAvailableError
from app.db.session import connection_ctx, get_connection, transaction
from app.services import gateway_report_relay as relay
from app.services.channels.whatsapp_personal_access import personal_owner

__all__ = ["channel_db", "receipt_db", "whatsapp_relay_db"]


async def planned_claim(f, attempts):
    db, clock = f.fixture.db, f.clock
    async with transaction(db.first):
        await db.first.execute(
            "UPDATE gateway_report_deliveries SET attempts=%(attempts)s WHERE id=%(id)s",
            {"attempts": attempts - 1, "id": f.intent.id},
        )
        claim = (await dao.claim_batch(limit=1, lease_seconds=60, now=clock()))[0]
        message, agent = await relay._source(claim)
        session = await relay._im_destination(claim, message)
        outbound = await relay.configured_outbound(agent, session)
        plan = relay.build_plan(outbound.target, "synthetic reply", claim.id)
        assert await dao.initialize_plan(claim, plan, now=clock())
    return replace(claim, im_plan=plan)


def synthetic_sender(db, monkeypatch):
    sent = []

    async def send(_outbound, chunk, **_kwargs):
        assert get_connection() is None
        assert db.first.raw.info.transaction_status == TransactionStatus.IDLE
        sent.append(chunk)

    monkeypatch.setattr(relay, "send_chunk", send)
    return sent


@pytest.mark.parametrize("attempts", [6, 10])
@pytest.mark.parametrize("stage", ["message", "session", "agent"])
async def test_presend_writer_defers_briefly_then_completes_once(whatsapp_relay_db, monkeypatch, stage, attempts):
    # Given a durable plan and an independent writer holding the actual identity row.
    f = whatsapp_relay_db
    db = f.fixture.db
    claim = await planned_claim(f, attempts)
    assert claim.im_plan is not None
    deferred = None
    sent = synthetic_sender(db, monkeypatch)
    query, row_id = {
        "message": ("SELECT id FROM gateway_messages WHERE id=%(id)s FOR UPDATE", claim.message_id),
        "session": ("SELECT id FROM chat_sessions WHERE id=%(id)s FOR UPDATE", claim.destination_id),
        "agent": ("SELECT id FROM agents WHERE id=%(id)s FOR UPDATE", f.fixture.config.agent_id),
    }[stage]
    held, release = anyio.Event(), anyio.Event()
    retry = dao.retry
    lock_identity = relay._lock_identity
    contended: list[relay.PresendIdentityContentionError] = []

    async def record_contention(current):
        try:
            await lock_identity(current)
        except relay.PresendIdentityContentionError as error:
            contended.append(error)
            raise

    async def after_rollback(current, *, available_at, now):
        assert get_connection() is None
        assert db.first.raw.info.transaction_status == TransactionStatus.IDLE
        return await retry(current, available_at=available_at, now=now)

    monkeypatch.setattr(dao, "retry", after_rollback)
    monkeypatch.setattr(relay, "_lock_identity", record_contention)

    async def holder():
        async with transaction(db.second):
            await db.second.fetchone(query, {"id": row_id})
            held.set()
            await release.wait()

    # When the real delivery encounters NOWAIT contention, then the writer releases.
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(holder)
            await held.wait()
            try:
                await relay.deliver_claim(claim, f.clock)
                deferred = await dao.get(claim.id)
            finally:
                release.set()
    # Then no send/progress occurred and cumulative provider attempts did not inflate the delay.
    assert sent == []
    assert deferred is not None
    assert deferred.available_at is not None
    assert timedelta(seconds=1) <= deferred.available_at - f.clock() <= timedelta(seconds=2)
    assert [error.stage for error in contended] == [stage]
    for error in contended:
        assert isinstance(error.__cause__, LockNotAvailableError)
        assert isinstance(error.__cause__.orig, LockNotAvailable)
        assert error.__cause__.orig.sqlstate == "55P03"
    assert (deferred.im_plan, deferred.next_chunk, deferred.attempts) == (claim.im_plan, claim.next_chunk, attempts)
    assert deferred.claim_token is deferred.lease_expires_at is deferred.delivered_at is deferred.terminal_at is None
    f.clock.now = deferred.available_at
    current = (await dao.claim_batch(limit=1, lease_seconds=60, now=f.clock()))[0]
    await relay.deliver_claim(current, f.clock)
    delivered = await dao.get(claim.id)
    assert delivered is not None
    assert delivered.delivered_at == f.clock()
    assert delivered.next_chunk == 1
    assert delivered.im_plan == claim.im_plan
    assert sent == list(claim.im_plan.chunks)


async def test_inverse_order_nowait_rollback_releases_session_for_owner_writer(whatsapp_relay_db, monkeypatch):
    # Given owner writes taking agent before session, opposite to relay identity validation.
    f = whatsapp_relay_db
    db = f.fixture.db
    claim = await planned_claim(f, 6)
    sent = synthetic_sender(db, monkeypatch)
    owner_locked, session_locked, writer_done = anyio.Event(), anyio.Event(), anyio.Event()
    fetchall = DbConnection.fetchall

    async def gated_agents(connection, query, params=None):
        if connection.raw is db.first.raw and "FROM agents" in query and "NOWAIT" in query:
            session_locked.set()
            while not await connection.fetchval(
                "SELECT cardinality(pg_blocking_pids(%(pid)s))", {"pid": db.second.raw.info.backend_pid}
            ):
                await anyio.lowlevel.checkpoint()
        return await fetchall(connection, query, params)

    monkeypatch.setattr(DbConnection, "fetchall", gated_agents)

    async def owner_write():
        async with transaction(db.second):
            await personal_owner(db.agent.id)
            owner_locked.set()
            await session_locked.wait()
            await db.second.fetchone(
                "SELECT id FROM chat_sessions WHERE id=%(id)s FOR UPDATE", {"id": claim.destination_id}
            )
        writer_done.set()

    # When the barrier proves the writer is blocked by the relay's session lock.
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(owner_write)
            await owner_locked.wait()
            await relay.deliver_claim(claim, f.clock)
            await writer_done.wait()
    # Then NOWAIT, not a deadlock victim or provider send, freed that lock and deferred briefly.
    row = await dao.get(claim.id)
    assert row is not None
    assert row.available_at is not None
    assert timedelta(seconds=1) <= row.available_at - f.clock() <= timedelta(seconds=2)
    assert row.next_chunk == 0
    assert sent == []


@pytest.mark.parametrize("attempts", [6, 10])
async def test_postsend_checkpoint_55p03_keeps_provider_backoff(whatsapp_relay_db, monkeypatch, attempts):
    # Given a synthetic provider ACK followed by an actual writer on the checkpoint row.
    f = whatsapp_relay_db
    db = f.fixture.db
    claim = await planned_claim(f, attempts)
    sent, held, release, finished = anyio.Event(), anyio.Event(), anyio.Event(), anyio.Event()
    checkpoint, retry = dao.checkpoint_chunk, dao.retry

    async def holder():
        await sent.wait()
        async with transaction(db.second):
            await db.second.fetchone(
                "SELECT id FROM gateway_report_deliveries WHERE id=%(id)s FOR UPDATE", {"id": claim.id}
            )
            held.set()
            await release.wait()
        finished.set()

    async def sender(*_args, **_kwargs):
        assert get_connection() is None
        sent.set()
        await held.wait()

    async def bounded_checkpoint(*args, **kwargs):
        async with connection_ctx() as db:
            await db.execute("SET LOCAL lock_timeout='50ms'")
            return await checkpoint(*args, **kwargs)

    async def released_retry(*args, **kwargs):
        assert get_connection() is None
        assert db.first.raw.info.transaction_status == TransactionStatus.IDLE
        release.set()
        await finished.wait()
        return await retry(*args, **kwargs)

    monkeypatch.setattr(relay, "send_chunk", sender)
    monkeypatch.setattr(dao, "checkpoint_chunk", bounded_checkpoint)
    monkeypatch.setattr(dao, "retry", released_retry)
    # When the real checkpoint UPDATE raises mapped 55P03 after the send boundary.
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(holder)
            await relay.deliver_claim(claim, f.clock)
    # Then it is not misclassified as pre-send contention or advanced without a checkpoint.
    row = await dao.get(claim.id)
    assert row is not None
    assert row.available_at == f.clock() + timedelta(seconds={6: 32, 10: 300}[attempts])
    assert row.next_chunk == 0
    assert row.im_plan == claim.im_plan
    assert row.attempts == attempts
