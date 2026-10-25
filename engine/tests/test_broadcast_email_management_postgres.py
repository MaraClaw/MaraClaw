from dataclasses import asdict
from datetime import timedelta
from uuid import uuid4

import anyio
import pytest

from app.dao.broadcast_email_dao import broadcast_email_dao as outbox
from app.db.session import connection_ctx
from app.records.broadcast_email import BroadcastEmailRecord
from broadcast_email_persistence_fixtures import PersistenceDB, broadcast_db, enqueue, tenant

__all__ = ["broadcast_db"]


async def test_status_counts_tokens_not_lease_expiry_and_hides_payload(broadcast_db: PersistenceDB) -> None:
    # Given: queued, live/expired claims, accepted SMTP and permanently failed recipients.
    rows = await enqueue(broadcast_db, 6)
    scope = rows[0]
    now = scope.created_at + timedelta(seconds=1)
    claims = await outbox.claim_batch(limit=4, lease_seconds=30, now=now)
    assert await outbox.mark_sent(claims[0], now=now)
    assert await outbox.fail(claims[1], error="private SMTP response", now=now)
    await broadcast_db.admin.execute(
        "UPDATE broadcast_emails SET lease_expires_at = %(expired)s WHERE id = %(id)s",
        {"expired": now - timedelta(seconds=1), "id": claims[2].id},
    )
    # When: an administrator reads a retained broadcast's aggregate.
    result = await outbox.email_status(scope.tenant_id, scope.broadcast_id)
    # Then: only the public counters are returned; expired tokens remain claimed.
    assert asdict(result) == {
        "broadcast_id": scope.broadcast_id,
        "available": True,
        "queued": 2,
        "claimed": 2,
        "smtp_accepted": 1,
        "failed": 1,
        "cancelled": 0,
    }


async def test_cancellation_only_settles_null_tokens_including_delayed_retries(broadcast_db: PersistenceDB) -> None:
    # Given: a delayed retry, a fresh queued row, expired/live tokens and settled rows.
    rows = await enqueue(broadcast_db, 6)
    scope = rows[0]
    now = scope.created_at + timedelta(seconds=1)
    claims = await outbox.claim_batch(limit=5, lease_seconds=30, now=now)
    assert await outbox.mark_sent(claims[0], now=now)
    assert await outbox.fail(claims[1], error="permanent", now=now)
    assert await outbox.retry(claims[2], error="temporary", available_at=now + timedelta(hours=1), now=now)
    await broadcast_db.admin.execute(
        "UPDATE broadcast_emails SET lease_expires_at = %(expired)s WHERE id = %(id)s",
        {"expired": now - timedelta(seconds=1), "id": claims[3].id},
    )
    # When: queued recipients are cancelled.
    result = await outbox.cancel_email(scope.tenant_id, scope.broadcast_id)
    # Then: post-operation counts preserve both claims and both other terminal states.
    assert result.cancelled_now == 2
    assert asdict(result.status) == {
        "broadcast_id": scope.broadcast_id,
        "available": True,
        "queued": 0,
        "claimed": 2,
        "smtp_accepted": 1,
        "failed": 1,
        "cancelled": 2,
    }
    cancelled = await broadcast_db.admin.fetchall("SELECT * FROM broadcast_emails WHERE status = 'cancelled'")
    assert len(cancelled) == 2
    assert all(BroadcastEmailRecord.from_row(row).settled_at is not None for row in cancelled)
    replay = await outbox.cancel_email(scope.tenant_id, scope.broadcast_id)
    assert replay.cancelled_now == 0
    assert replay.status == result.status


async def test_missing_and_foreign_status_and_cancellation_are_uniform(broadcast_db: PersistenceDB) -> None:
    # Given: a real broadcast belonging to another current tenant.
    [scope] = await enqueue(broadcast_db)
    foreign_tenant = await tenant(broadcast_db)
    for tenant_id, broadcast_id in [(foreign_tenant, scope.broadcast_id), (scope.tenant_id, uuid4())]:
        # When: read and cancellation target a scope with no retained rows.
        status = await outbox.email_status(tenant_id, broadcast_id)
        cancelled = await outbox.cancel_email(tenant_id, broadcast_id)
        # Then: neither operation discloses existence or recipient state.
        assert cancelled.status == status
        assert asdict(status) == {
            "broadcast_id": broadcast_id,
            "available": False,
            "queued": 0,
            "claimed": 0,
            "smtp_accepted": 0,
            "failed": 0,
            "cancelled": 0,
        }
        assert cancelled.cancelled_now == 0
    assert (await outbox.email_status(scope.tenant_id, scope.broadcast_id)).queued == 1


async def test_cancellation_commits_despite_ambient_rollback_and_restart(broadcast_db: PersistenceDB) -> None:
    # Given: an unrelated request transaction destined to roll back.
    [scope] = await enqueue(broadcast_db)

    async def cancel_then_rollback() -> None:
        async with connection_ctx():
            assert (await outbox.cancel_email(scope.tenant_id, scope.broadcast_id)).cancelled_now == 1
            raise RuntimeError("ambient rollback")

    # When: cancellation completes inside that ambient scope.
    with pytest.raises(RuntimeError, match="ambient rollback"):
        await cancel_then_rollback()
    await broadcast_db.restart()
    # Then: cancellation remains durable and no worker can claim the row.
    assert (await outbox.email_status(scope.tenant_id, scope.broadcast_id)).cancelled == 1
    assert await outbox.claim_batch(limit=5, lease_seconds=30, now=scope.created_at + timedelta(seconds=1)) == []


async def test_claim_and_cancel_race_never_cancel_a_committed_token(broadcast_db: PersistenceDB) -> None:
    # Given: concurrent claim/cancel contenders for a single durable broadcast.
    rows = await enqueue(broadcast_db, 8)
    scope = rows[0]
    now = scope.created_at + timedelta(seconds=1)
    claims: list[BroadcastEmailRecord] = []
    cancelled: list[int] = []

    async def claim() -> None:
        claims.extend(await outbox.claim_batch(limit=8, lease_seconds=30, now=now))

    async def cancel() -> None:
        cancelled.append((await outbox.cancel_email(scope.tenant_id, scope.broadcast_id)).cancelled_now)

    # When: independent PostgreSQL transactions contend on the same rows.
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(claim)
            tasks.start_soon(cancel)
    # Then: each row is exclusively claimed or cancelled, never both.
    status = await outbox.email_status(scope.tenant_id, scope.broadcast_id)
    assert status.claimed == len(claims)
    assert status.cancelled == sum(cancelled)
    assert status.claimed + status.cancelled == 8
    for row in claims:
        assert await outbox.mark_sent(row, now=now)
