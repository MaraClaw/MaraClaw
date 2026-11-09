import anyio
import pytest

from app.api import notification as api
from app.core.domain.notifications import BroadcastJobStatus
from app.dao.broadcast_job_batch import process_broadcast_batch
from app.db.connection import DbConnection, Params
from app.db.errors import LockNotAvailableError
from app.db.pool import get_pool
from app.services import broadcast_fanout_worker as worker
from broadcast_email_persistence_fixtures import PersistenceDB, tenant
from broadcast_job_fixtures import accept, admin, agent


@pytest.mark.parametrize("mutation", ["move", "inactive", "delete"])
async def test_membership_change_while_batch_waits_on_target_lock_is_skipped(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    sender = await admin(broadcast_db, 1)
    receipt = await accept(sender)
    target = await broadcast_db.admin.fetchval("SELECT target_id FROM broadcast_job_recipients")
    destination = await tenant(broadcast_db)
    locking_target = anyio.Event()
    execute = DbConnection.execute

    async def observe_lock(db: DbConnection, query: str, params: Params = None) -> None:
        if "FOR UPDATE OF target" in query:
            locking_target.set()
        await execute(db, query, params)

    monkeypatch.setattr(DbConnection, "execute", observe_lock)

    async def batch() -> None:
        assert await process_broadcast_batch(limit=1)

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks, broadcast_db.admin.raw.transaction():
            match mutation:
                case "move":
                    await broadcast_db.admin.execute(
                        "UPDATE users SET tenant_id = %(tenant)s WHERE id = %(id)s",
                        {"tenant": destination, "id": target},
                    )
                case "inactive":
                    await broadcast_db.admin.execute(
                        "UPDATE users SET is_active = false WHERE id = %(id)s", {"id": target}
                    )
                case "delete":
                    await broadcast_db.admin.execute("DELETE FROM users WHERE id = %(id)s", {"id": target})
            tasks.start_soon(batch)
            await locking_target.wait()
    status = await api.broadcast_job_status(receipt["broadcast_id"], sender)
    assert (status["status"], status["processed"], status["skipped"], status["users_notified"]) == (
        BroadcastJobStatus.COMPLETED,
        1,
        1,
        0,
    )
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM notifications") == 0


async def test_batch_cancellation_rolls_back_and_restarts_without_duplicate_effects(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = await admin(broadcast_db, 2)
    receipt = await accept(sender)
    wrote_effects = anyio.Event()
    execute = DbConnection.execute

    async def pause_after_effects(db: DbConnection, query: str, params: Params = None) -> None:
        await execute(db, query, params)
        if "WITH batch AS MATERIALIZED" in query:
            wrote_effects.set()
            await anyio.sleep_forever()

    async def batch() -> None:
        _ = await process_broadcast_batch(limit=2)

    with monkeypatch.context() as patch:
        patch.setattr(DbConnection, "execute", pause_after_effects)
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(batch)
                await wrote_effects.wait()
                tasks.cancel_scope.cancel()
    status = await api.broadcast_job_status(receipt["broadcast_id"], sender)
    assert (status["processed"], status["status"]) == (0, BroadcastJobStatus.PENDING)
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM notifications") == 0
    await broadcast_db.restart()
    assert await process_broadcast_batch(limit=2)
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM notifications") == 2
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM broadcast_history") == 1


@pytest.mark.parametrize("kind", ["user", "agent"])
async def test_captured_target_cannot_rejoin_during_batch(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    sender = await admin(broadcast_db, 1 if kind == "user" else 0)
    if kind == "agent":
        _ = await agent(broadcast_db, sender)
    receipt = await accept(sender)
    target = await broadcast_db.admin.fetchval("SELECT target_id FROM broadcast_job_recipients")
    destination = await tenant(broadcast_db)
    table = "users" if kind == "user" else "agents"
    move_target = {
        "user": "UPDATE users SET tenant_id = %(tenant)s WHERE id = %(id)s",
        "agent": "UPDATE agents SET tenant_id = %(tenant)s WHERE id = %(id)s",
    }[kind]
    await broadcast_db.admin.execute(move_target, {"tenant": destination, "id": target})
    locked = anyio.Event()
    proceed = anyio.Event()
    execute = DbConnection.execute

    async def pause_after_locks(db: DbConnection, query: str, params: Params = None) -> None:
        await execute(db, query, params)
        if "FOR UPDATE OF target" in query and f"FROM {table} target" in query:
            locked.set()
            await proceed.wait()

    monkeypatch.setattr(DbConnection, "execute", pause_after_locks)

    async def batch() -> None:
        assert await process_broadcast_batch(limit=1)

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(batch)
            await locked.wait()
            async with get_pool().connection() as raw, raw.transaction(force_rollback=True):
                db = DbConnection(raw)
                await db.execute("SET LOCAL lock_timeout = '100ms'")
                with pytest.raises(LockNotAvailableError):
                    await db.execute(move_target, {"tenant": sender.tenant_id, "id": target})
            proceed.set()
    status = await api.broadcast_job_status(receipt["broadcast_id"], sender)
    assert (status["processed"], status["skipped"]) == (1, 1)
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM notifications") == 0


async def test_fanout_loop_resumes_after_timed_out_batch(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = await admin(broadcast_db, 1)
    receipt = await accept(sender)
    completed = anyio.Event()
    attempts = 0

    async def observe_batch(*, limit: int) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await anyio.sleep_forever()
        worked = await process_broadcast_batch(limit=limit)
        if worked:
            completed.set()
        return worked

    monkeypatch.setattr(worker, "BATCH_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(worker, "process_broadcast_batch", observe_batch)
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(worker.start_broadcast_fanout_worker)
            await completed.wait()
            tasks.cancel_scope.cancel()
    status = await api.broadcast_job_status(receipt["broadcast_id"], sender)
    assert (status["status"], status["users_notified"]) == (BroadcastJobStatus.COMPLETED, 1)
    assert attempts >= 2
