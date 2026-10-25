from dataclasses import replace
from datetime import timedelta

import pytest

from app.dao.broadcast_email_dao import broadcast_email_dao as outbox
from app.db.errors import CheckViolationError, UniqueViolationError
from app.db.session import connection_ctx
from app.records.broadcast_email import NewBroadcastEmail
from broadcast_email_persistence_fixtures import PersistenceDB, broadcast_db, enqueue

__all__ = ["broadcast_db"]


async def test_retry_backoff_and_five_attempt_limit(broadcast_db: PersistenceDB):
    [original] = await enqueue(broadcast_db)
    now = original.created_at + timedelta(seconds=1)
    for attempt in range(1, 6):
        [claim] = await outbox.claim_batch(limit=1, lease_seconds=30, now=now)
        assert claim.attempts == attempt
        later = now + timedelta(seconds=10)
        assert await outbox.retry(claim, error="smtp_temporary", available_at=later, now=now)
        assert await outbox.claim_batch(limit=1, lease_seconds=30, now=later - timedelta(microseconds=1)) == []
        now = later
    assert await outbox.claim_batch(limit=1, lease_seconds=30, now=now) == []
    row = await broadcast_db.admin.fetchone("SELECT status, attempts, last_error FROM broadcast_emails")
    assert row == {"status": "failed", "attempts": 5, "last_error": "smtp_temporary"}


@pytest.mark.parametrize("after_deadline", [0, 1])
async def test_retry_terminalizes_when_next_attempt_misses_deadline(broadcast_db: PersistenceDB, after_deadline: int):
    [original] = await enqueue(broadcast_db)
    now = original.deliver_by - timedelta(seconds=30)
    [claim] = await outbox.claim_batch(limit=1, lease_seconds=60, now=now)
    assert await outbox.retry(
        claim, error="smtp_temporary", available_at=original.deliver_by + timedelta(seconds=after_deadline), now=now
    )
    assert await broadcast_db.admin.fetchval("SELECT status FROM broadcast_emails") == "failed"


async def test_expiration_sweep_is_bounded_and_never_steals_active_lease(broadcast_db: PersistenceDB):
    rows = await enqueue(broadcast_db, 6)
    now = rows[0].created_at + timedelta(seconds=1)
    [active] = await outbox.claim_batch(limit=1, lease_seconds=60, now=now)
    await broadcast_db.admin.execute("UPDATE broadcast_emails SET attempts = 5, deliver_by = %(now)s", {"now": now})
    assert await outbox.claim_batch(limit=2, lease_seconds=30, now=now) == []
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM broadcast_emails WHERE status = 'failed'") == 2
    assert (
        await broadcast_db.admin.fetchval("SELECT status FROM broadcast_emails WHERE id = %(id)s", {"id": active.id})
        == "pending"
    )


async def test_expired_fifth_attempt_becomes_terminal_instead_of_reclaimed(broadcast_db: PersistenceDB):
    [original] = await enqueue(broadcast_db)
    now = original.created_at + timedelta(seconds=1)
    await broadcast_db.admin.execute("UPDATE broadcast_emails SET attempts = 4")
    [claim] = await outbox.claim_batch(limit=1, lease_seconds=10, now=now)
    assert claim.attempts == 5
    assert await outbox.claim_batch(limit=1, lease_seconds=10, now=now + timedelta(seconds=10)) == []
    assert await broadcast_db.admin.fetchone("SELECT status, attempts, last_error FROM broadcast_emails") == {
        "status": "failed",
        "attempts": 5,
        "last_error": "attempt_limit",
    }


async def test_unleased_deadline_expires_even_when_next_attempt_is_not_due(broadcast_db: PersistenceDB):
    [original] = await enqueue(broadcast_db)
    now = original.deliver_by
    await broadcast_db.admin.execute(
        "UPDATE broadcast_emails SET available_at = %(later)s", {"later": now + timedelta(hours=1)}
    )
    assert await outbox.claim_batch(limit=1, lease_seconds=30, now=now) == []
    assert await broadcast_db.admin.fetchone("SELECT status, attempts, last_error FROM broadcast_emails") == {
        "status": "failed",
        "attempts": 0,
        "last_error": "delivery_deadline",
    }


async def test_recipient_uniqueness_rolls_back_the_entire_enqueue_batch(broadcast_db: PersistenceDB):
    [original] = await enqueue(broadcast_db)
    duplicate = NewBroadcastEmail(
        original.broadcast_id, original.tenant_id, original.user_id, "duplicate@example.test", "Subject", "Body"
    )
    different = replace(duplicate, user_id=original.id)
    with pytest.raises(UniqueViolationError):
        async with connection_ctx():
            _ = await outbox.enqueue_many([different, duplicate])
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM broadcast_emails") == 1


async def test_retention_purges_only_bounded_old_terminal_rows(broadcast_db: PersistenceDB):
    rows = await enqueue(broadcast_db, 5)
    now = rows[0].created_at + timedelta(seconds=1)
    claims = await outbox.claim_batch(limit=4, lease_seconds=30, now=now)
    assert await outbox.mark_sent(claims[0], now=now)
    assert await outbox.fail(claims[1], error="permanent", now=now)
    assert await outbox.fail(claims[2], error="recent", now=now + timedelta(seconds=5))
    assert (await outbox.cancel_email(rows[0].tenant_id, rows[0].broadcast_id)).cancelled_now == 1
    cutoff = now + timedelta(seconds=1)
    assert await outbox.purge_settled(older_than=cutoff, limit=1) == 1
    assert await outbox.purge_settled(older_than=cutoff, limit=1) == 1
    assert await outbox.purge_settled(older_than=cutoff, limit=10) == 1
    assert await outbox.purge_settled(older_than=cutoff, limit=10) == 0
    assert await broadcast_db.admin.fetchone(
        "SELECT count(*) FILTER (WHERE status = 'pending') AS pending, "
        "count(*) FILTER (WHERE status = 'failed') AS failed FROM broadcast_emails"
    ) == {"pending": 1, "failed": 1}


async def test_cancelled_requires_settlement_and_retention_skips_locked_rows(broadcast_db: PersistenceDB):
    # Given: cancellation is terminal, and one old terminal row is locked elsewhere.
    rows = await enqueue(broadcast_db, 2)
    with pytest.raises(CheckViolationError):
        await broadcast_db.admin.execute("UPDATE broadcast_emails SET status = 'cancelled'")
    _ = await outbox.cancel_email(rows[0].tenant_id, rows[0].broadcast_id)
    cutoff = rows[0].created_at + timedelta(days=1)
    # When: bounded retention encounters a locked cancellation.
    async with connection_ctx() as db:
        await db.execute("SELECT id FROM broadcast_emails WHERE id = %(id)s FOR UPDATE", {"id": rows[0].id})
        assert await outbox.purge_settled(older_than=cutoff, limit=1) == 1
    # Then: the locked row survives until a later purge, after which status is unavailable.
    assert (await outbox.email_status(rows[0].tenant_id, rows[0].broadcast_id)).cancelled == 1
    assert await outbox.purge_settled(older_than=cutoff, limit=1) == 1
    assert not (await outbox.email_status(rows[0].tenant_id, rows[0].broadcast_id)).available


async def test_retry_fail_and_purge_do_not_join_ambient_transaction(broadcast_db: PersistenceDB):
    rows = await enqueue(broadcast_db, 3)
    now = rows[0].created_at + timedelta(seconds=1)
    first, second, third = await outbox.claim_batch(limit=3, lease_seconds=30, now=now)
    assert await outbox.mark_sent(third, now=now)

    async def rolled_back_request() -> None:
        async with connection_ctx():
            assert await outbox.retry(first, error="retry", available_at=now + timedelta(seconds=5), now=now)
            assert await outbox.fail(second, error="permanent", now=now + timedelta(seconds=2))
            assert await outbox.purge_settled(older_than=now + timedelta(seconds=1), limit=1) == 1
            raise RuntimeError("ambient rollback")

    with pytest.raises(RuntimeError, match="ambient rollback"):
        await rolled_back_request()
    assert await broadcast_db.admin.fetchone(
        "SELECT count(*) FILTER (WHERE status = 'pending') AS pending, "
        "count(*) FILTER (WHERE status = 'failed') AS failed FROM broadcast_emails"
    ) == {"pending": 1, "failed": 1}
