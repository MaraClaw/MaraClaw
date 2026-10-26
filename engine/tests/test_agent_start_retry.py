from dataclasses import replace

import anyio
import pytest

import test_agent_start_postgres as fixtures
from app.dao.agent_start_dao import agent_start_dao
from app.dao.agent_start_retry_dao import agent_start_retry_dao
from app.db.connection import DbConnection
from app.db.session import connection_ctx
from app.records.agent_start import AgentStartOperation
from app.records.agent_start_retry import AgentStartRetryMetadata
from app.services import agent_start_reconciler as reconciler
from app.services.agent_start_store import PostgresStartStore
from app.services.guest_admission import GuestAdmissionError, GuestStartPreflightError
from test_openclaw_update_postgres import publish, runs

db = fixtures.db
start_db = fixtures.start_db


async def make_pending(db):
    return await fixtures.pending_start(db)


async def retry_metadata(db: DbConnection, operation: AgentStartOperation) -> AgentStartRetryMetadata:
    result = await agent_start_retry_dao.get(db, operation.operation_id)
    assert result is not None
    return result


async def due_now(db, operation):
    await db.execute(
        "UPDATE agent_start_retry_metadata SET next_retry_at=now()-interval '1 second' WHERE operation_id=%(op)s",
        {"op": operation.operation_id},
    )


@pytest.fixture
def executor(monkeypatch, tmp_path):
    calls = []

    async def start(record):
        calls.append(record.id)
        record.status, record.container_id = "running", str(record.id)
        return record.container_id

    monkeypatch.setattr(reconciler.agent_manager, "start_container", start)
    monkeypatch.setattr(reconciler.agent_manager, "_agent_dir", lambda identifier: tmp_path / str(identifier))
    return calls


async def test_scan_bounds_due_work_and_leaves_future_retry_untouched(start_db, executor):
    # Given ten due intents and one future retry on independently committed connections.
    pending = [await make_pending(start_db) for _ in range(11)]
    future = pending[-1][0]
    await start_db.execute(
        "UPDATE agent_start_retry_metadata SET next_retry_at=now()+interval '1 hour' WHERE operation_id=%(op)s",
        {"op": future.operation_id},
    )
    # When consecutive scans execute.
    await reconciler.reconcile_agent_starts_once()
    first = list(executor)
    await reconciler.reconcile_agent_starts_once()
    # Then the first scan is bounded and the second finishes only the remaining due work.
    assert first == [record.id for _, record in pending[:8]]
    assert executor == [record.id for _, record in pending[:10]]
    assert (await retry_metadata(start_db, future)).attempts == 0


async def test_failed_attempts_reserve_increasing_database_backoff_before_docker(start_db, executor, monkeypatch):
    # Given a capacity refusal whose execution observes committed retry reservations.
    operation, original = await make_pending(start_db)
    observations = []

    async def refuse(_record):
        observations.append(await agent_start_retry_dao.get(start_db, operation.operation_id))
        raise GuestAdmissionError("guest_memory_capacity_exceeded", "not persisted")

    monkeypatch.setattr(reconciler.agent_manager, "start_container", refuse)
    # When one scan fails, another immediate scan runs, then a later due retry fails.
    await reconciler.reconcile_agent_starts_once()
    await reconciler.reconcile_agent_starts_once()
    await due_now(start_db, operation)
    await reconciler.reconcile_agent_starts_once()
    # Then no same-scan retry occurs, history is durable and the second delay is twenty seconds.
    assert len(observations) == 2
    for attempt, metadata in enumerate(observations, 1):
        assert metadata is not None
        assert metadata.attempts == attempt
        assert metadata.next_retry_at is not None
        assert metadata.last_attempt_at is not None
        assert (metadata.next_retry_at - metadata.last_attempt_at).total_seconds() == 10 * attempt
    metadata = await retry_metadata(start_db, operation)
    assert metadata.last_failure_code == "capacity_exceeded"
    current = await agent_start_dao.get(start_db, original.id)
    assert current is not None
    assert current.state == "pending"


async def test_explicit_retry_reuses_pending_identity_and_preserves_history(start_db, executor, monkeypatch):
    # Given a failed intent with a future retry reservation.
    operation, original = await make_pending(start_db)
    store = PostgresStartStore()
    assert await store.reserve(operation)
    await store.fail(operation, "readiness_failed")
    # When an explicit Start retries before its due time.
    pending = await store.begin(original.id)
    assert pending is not None
    replay, fresh = pending
    assert await reconciler.execute_start(replay, fresh)
    # Then the same identity publishes with its prior attempt retained and scheduling cleared.
    metadata = await retry_metadata(start_db, operation)
    assert replay == operation
    assert metadata.attempts == 2
    assert metadata.next_retry_at is None


async def test_backoff_caps_at_three_hundred_seconds(start_db):
    # Given a pending operation retried repeatedly by explicit requests.
    operation, _original = await make_pending(start_db)
    delays = []
    # When the database reserves seven attempts.
    for _ in range(7):
        assert await PostgresStartStore().reserve(operation)
        metadata = await retry_metadata(start_db, operation)
        assert metadata.next_retry_at is not None
        assert metadata.last_attempt_at is not None
        delays.append((metadata.next_retry_at - metadata.last_attempt_at).total_seconds())
    # Then the database schedule follows the capped exponential sequence.
    assert delays == [10, 20, 40, 80, 160, 300, 300]


async def test_configuration_failure_requires_intervention_without_tight_retry(start_db, executor, monkeypatch):
    # Given typed configuration failure contains private runtime context.
    operation, _original = await make_pending(start_db)

    async def refuse(_record):
        raise GuestStartPreflightError("secret configuration context")

    monkeypatch.setattr(reconciler.agent_manager, "start_container", refuse)
    # When the scan fails and is immediately repeated.
    await reconciler.reconcile_agent_starts_once()
    await reconciler.reconcile_agent_starts_once()
    # Then history exposes only the safe code and remains pending but unscheduled.
    metadata = await retry_metadata(start_db, operation)
    assert (metadata.attempts, metadata.last_failure_code, metadata.next_retry_at) == (1, "configuration_failed", None)
    assert await PostgresStartStore().outstanding() == []


async def test_upgrade_defers_without_attempt_and_other_agent_executes(start_db, executor):
    # Given the first pending start has a real active upgrade and another start is due.
    first, first_agent = await make_pending(start_db)
    _second, second_agent = await make_pending(start_db)
    notice, _created = await publish(False)
    assert await runs.create_pending(notice_id=notice.id, agent_id=first_agent.id, requested_by=first_agent.creator_id)
    # When the actual bounded worker scan runs twice.
    await reconciler.reconcile_agent_starts_once()
    await reconciler.reconcile_agent_starts_once()
    # Then the upgrade keeps its start intent without a Docker attempt and never kills the loop.
    metadata = await retry_metadata(start_db, first)
    assert (metadata.attempts, metadata.last_failure_code) == (0, "upgrade_in_progress")
    assert metadata.next_retry_at is not None
    current = await agent_start_dao.get(start_db, first.agent_id)
    assert current is not None
    assert current.state == "pending"
    assert executor == [second_agent.id]


async def test_due_recheck_after_flock_prevents_two_workers_attempting_once(start_db, executor):
    # Given two workers selected the same due operation before acquiring its flock.
    operation, _original = await make_pending(start_db)
    # When the workers contend through the real lock and independent connections.
    async with anyio.create_task_group() as group:
        group.start_soon(reconciler.reconcile_agent_starts_once)
        group.start_soon(reconciler.reconcile_agent_starts_once)
    # Then one attempt publishes and the second cannot execute its stale selection.
    assert executor == [operation.agent_id]
    assert (await retry_metadata(start_db, operation)).attempts == 1


async def test_cancel_and_replacement_clear_schedule_and_reject_stale_cas(start_db):
    # Given a reserved start attempt.
    operation, original = await make_pending(start_db)
    store = PostgresStartStore()
    assert await store.reserve(operation)
    # When another transaction cancels then replaces its agent-keyed operation.
    await store.cancel(original.id)
    cancelled = await retry_metadata(start_db, operation)
    pending = await store.begin(original.id)
    assert pending is not None
    replacement, _record = pending
    # Then cancellation cleared scheduling atomically, superseded metadata is pruned and stale CAS fails.
    assert cancelled.next_retry_at is None
    assert replacement.operation_id != operation.operation_id
    assert await agent_start_retry_dao.get(start_db, operation.operation_id) is None
    assert not await store.reserve(operation)
    assert not await store.publish(operation, replace(original, status="running", container_id="stale"))


async def test_reservation_survives_outer_transaction_rollback(start_db):
    # Given an existing operation and a request transaction that will roll back.
    operation, _original = await make_pending(start_db)

    async def request():
        async with connection_ctx():
            assert await PostgresStartStore().reserve(operation)
            raise ValueError("request disconnected")

    # When reservation commits independently before the request disappears.
    with pytest.raises(ValueError):
        await request()
    # Then a new connection reads the durable attempt and reservation.
    assert (await retry_metadata(start_db, operation)).attempts == 1


async def test_postlock_due_recheck_avoids_duplicate_failed_attempt(start_db, executor, monkeypatch):
    # Given workers selected the same pending operation before the first failure moved its due time.
    operation, _original = await make_pending(start_db)
    attempts = []

    async def refuse(_record):
        attempts.append(operation.agent_id)
        raise GuestAdmissionError("guest_memory_capacity_exceeded", "capacity")

    monkeypatch.setattr(reconciler.agent_manager, "start_container", refuse)
    # When two reconcilers contend with the same stale due selection.
    async with anyio.create_task_group() as group:
        group.start_soon(reconciler.reconcile_agent_starts_once)
        group.start_soon(reconciler.reconcile_agent_starts_once)
    # Then the loser rechecks the committed reservation after flock and performs no second attempt.
    assert attempts == [operation.agent_id]
    assert (await retry_metadata(start_db, operation)).attempts == 1


async def test_cancelled_cleanup_has_separate_bounded_fair_budget(start_db, executor, monkeypatch):
    # Given ten cancelled guests needing unavailable Docker and ten independently due starts.
    cancelled = [await make_pending(start_db) for _ in range(10)]
    for operation, _record in cancelled:
        await PostgresStartStore().cancel(operation.agent_id)
    pending = [await make_pending(start_db) for _ in range(10)]
    monkeypatch.setattr(reconciler.agent_manager, "docker_client", None)
    cleanup = reconciler.cleanup_cancelled_start
    visited = []

    async def observe(operation):
        visited.append(operation.agent_id)
        await cleanup(operation)

    monkeypatch.setattr(reconciler, "cleanup_cancelled_start", observe)
    # When two bounded scans run despite unsuccessful cancelled cleanup.
    await reconciler.reconcile_agent_starts_once()
    first_cleanup, first_starts = len(visited), len(executor)
    await reconciler.reconcile_agent_starts_once()
    # Then cancellation cleanup never consumes start capacity and rotates instead of starving later rows.
    assert (first_cleanup, first_starts) == (8, 8)
    assert len(visited) == 16
    assert set(visited) == {operation.agent_id for operation, _record in cancelled}
    assert executor == [record.id for _operation, record in pending]
