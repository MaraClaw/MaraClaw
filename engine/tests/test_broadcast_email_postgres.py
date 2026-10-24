from collections.abc import Sequence
from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from fastapi import Depends, FastAPI
from psycopg.errors import ForeignKeyViolation
from starlette.types import Message, Scope

from app.api import notification as notification_api
from app.core.security import get_current_user
from app.dao.broadcast_email_dao import broadcast_email_dao as outbox
from app.db.pool import get_pool
from app.db.session import bind_crud_connection, connection_ctx
from app.records.broadcast_email import BroadcastEmailRecord, NewBroadcastEmail
from broadcast_email_persistence_fixtures import (
    PersistenceDB,
    broadcast_db as broadcast_db,
    enqueue,
    member,
    tenant,
)


@pytest.mark.parametrize("failure", ["after_enqueue", "after_notifications"])
async def test_api_rolls_back_all_acceptance_on_later_failure(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch, failure: str
):
    tenant_id = await tenant(broadcast_db)
    sender = await member(broadcast_db, tenant_id)
    sender.role = "org_admin"
    _ = await member(broadcast_db, tenant_id)
    _ = await member(broadcast_db, tenant_id)
    monkeypatch.setattr(
        "app.services.system_email_service.resolve_email_config_async", AsyncMock(return_value=object())
    )
    enqueue_original = outbox.enqueue_many

    async def fail_after_enqueue(items: Sequence[NewBroadcastEmail]) -> int:
        _ = await enqueue_original(items)
        raise RuntimeError("later acceptance failure")

    if failure == "after_enqueue":
        monkeypatch.setattr(outbox, "enqueue_many", fail_after_enqueue)
    else:
        monkeypatch.setattr(
            notification_api.agent_dao,
            "list_for_tenant",
            AsyncMock(side_effect=RuntimeError("later acceptance failure")),
        )
    app = FastAPI()
    app.include_router(notification_api.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
    app.dependency_overrides[get_current_user] = lambda: sender
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        with pytest.raises(RuntimeError, match="later acceptance failure"):
            _ = await client.post("/api/notifications/broadcast", json={"title": "Update", "send_email": True})
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM notifications") == 0
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM broadcast_emails") == 0


@pytest.mark.parametrize("commit_fails", [False, True])
async def test_api_commits_acceptance_before_response_body(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch, commit_fails: bool
) -> None:
    # Given the production request transaction and an independent observer connection.
    tenant_id = await tenant(broadcast_db)
    sender = await member(broadcast_db, tenant_id)
    sender.role = "org_admin"
    _ = await member(broadcast_db, tenant_id)
    monkeypatch.setattr(
        "app.services.system_email_service.resolve_email_config_async", AsyncMock(return_value=object())
    )
    if commit_fails:
        # The random broadcast ID has no tenant: INSERT succeeds, but COMMIT must fail.
        await broadcast_db.admin.execute(
            "ALTER TABLE broadcast_emails ADD CONSTRAINT reject_broadcast_commit "
            "FOREIGN KEY (broadcast_id) REFERENCES tenants(id) DEFERRABLE INITIALLY DEFERRED"
        )
    app = FastAPI()
    app.include_router(notification_api.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
    app.dependency_overrides[get_current_user] = lambda: sender
    statuses: list[int] = []
    bodies: list[bytes] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b'{"title":"Update","send_email":true}', "more_body": False}

    async def send(message: Message) -> None:
        match message:
            case {"type": "http.response.start", "status": int(status)}:
                statuses.append(status)
            case {"type": "http.response.body", "body": bytes(body)}:
                bodies.append(body)
                # Observe before dependency teardown, not after ASGITransport returns.
                count = 0 if commit_fails else 1
                assert await broadcast_db.admin.fetchone(
                    "SELECT (SELECT count(*) FROM notifications) AS notifications, "
                    "(SELECT count(*) FROM broadcast_emails) AS emails"
                ) == {"notifications": count, "emails": count}

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/notifications/broadcast",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "server": ("test", 80),
        "client": ("test", 123),
    }
    # When the raw ASGI app sends the response, a failed commit must still permit HTTP 500.
    with pytest.raises(ForeignKeyViolation) if commit_fails else nullcontext():
        await app(scope, receive, send)
    app.dependency_overrides.clear()

    # Then only durable acceptance can be acknowledged as successful.
    assert statuses == [500 if commit_fails else 200]
    assert len(bodies) == 1


async def test_api_accepts_only_selected_recipient_snapshots(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch
):
    tenant_id = await tenant(broadcast_db)
    sender = await member(broadcast_db, tenant_id)
    sender.role = "org_admin"
    selected = await member(broadcast_db, tenant_id)
    _ = await member(broadcast_db, tenant_id, active=False)
    _ = await member(broadcast_db, await tenant(broadcast_db))
    monkeypatch.setattr(
        "app.services.system_email_service.resolve_email_config_async", AsyncMock(return_value=object())
    )
    result = await notification_api.broadcast_notification(
        notification_api.BroadcastRequest(title="Update", body="Details", send_email=True), current_user=sender
    )
    rows = await broadcast_db.admin.fetchall("SELECT * FROM broadcast_emails")
    assert result == {
        "ok": True,
        "broadcast_id": rows[0]["broadcast_id"],
        "users_notified": 1,
        "agents_notified": 0,
        "emails_sent": 1,
    }
    assert [(row["user_id"], row["tenant_id"], row["subject"], row["body"]) for row in rows] == [
        (selected.id, tenant_id, "Update", "Details\n\nSent by: Alice")
    ]
    assert await broadcast_db.admin.fetchval("SELECT user_id FROM notifications") == selected.id


async def test_restart_preserves_snapshot_and_message_identity(broadcast_db: PersistenceDB):
    [original] = await enqueue(broadcast_db)
    old_pool = get_pool()
    await broadcast_db.restart()
    [claim] = await outbox.claim_batch(limit=1, lease_seconds=30, now=original.created_at + timedelta(seconds=1))
    assert get_pool() is not old_pool
    assert (claim.to_email, claim.subject, claim.body) == ("recipient0@example.test", "Subject", "Body")
    assert claim.id == original.id
    assert claim.message_id == f"<{original.id}.broadcast@maraclaw.local>"
    assert original.deliver_by - original.created_at == timedelta(hours=24)


async def test_concurrent_claims_are_exclusive_and_bounded(broadcast_db: PersistenceDB):
    rows = await enqueue(broadcast_db, 7)
    now = rows[0].created_at + timedelta(seconds=1)
    batches: list[list[BroadcastEmailRecord]] = []

    async def claim() -> None:
        batches.append(await outbox.claim_batch(limit=2, lease_seconds=30, now=now))

    async with anyio.create_task_group() as tasks:
        for _ in range(3):
            tasks.start_soon(claim)
    ids = [row.id for batch in batches for row in batch]
    assert [len(batch) for batch in batches] == [2, 2, 2]
    assert len(set(ids)) == 6
    assert all(row.attempts == 1 and row.claim_token is not None for batch in batches for row in batch)


async def test_claim_skips_row_locked_by_another_transaction(broadcast_db: PersistenceDB):
    rows = await enqueue(broadcast_db, 2)
    async with connection_ctx() as db:
        await db.execute("SELECT id FROM broadcast_emails WHERE id = %(id)s FOR UPDATE", {"id": rows[0].id})
        with anyio.fail_after(2):
            claims = await outbox.claim_batch(limit=2, lease_seconds=30, now=rows[0].created_at + timedelta(seconds=1))
    assert [claim.id for claim in claims] == [rows[1].id]


@pytest.mark.parametrize("reclaimed", [False, True])
async def test_expired_claim_cannot_settle_before_or_after_reclaim(broadcast_db: PersistenceDB, reclaimed: bool):
    [original] = await enqueue(broadcast_db)
    now = original.created_at + timedelta(seconds=1)
    [stale] = await outbox.claim_batch(limit=1, lease_seconds=10, now=now)
    expired = now + timedelta(seconds=10)
    if reclaimed:
        [replacement] = await outbox.claim_batch(limit=1, lease_seconds=10, now=expired)
        assert replacement.claim_token != stale.claim_token
        assert replacement.attempts == 2
        assert replacement.message_id == stale.message_id
    assert not await outbox.mark_sent(stale, now=expired)
    assert not await outbox.retry(stale, error="stale", available_at=expired, now=expired)
    assert not await outbox.fail(stale, error="stale", now=expired)


async def test_settlements_commit_independently_of_ambient_rollback(broadcast_db: PersistenceDB):
    [original] = await enqueue(broadcast_db)
    now = original.created_at + timedelta(seconds=1)

    async def rolled_back_request() -> None:
        async with connection_ctx():
            [claim] = await outbox.claim_batch(limit=1, lease_seconds=30, now=now)
            assert await outbox.mark_sent(claim, now=now)
            raise RuntimeError("request rollback")

    with pytest.raises(RuntimeError, match="request rollback"):
        await rolled_back_request()
    assert await broadcast_db.admin.fetchval("SELECT status FROM broadcast_emails") == "sent"


async def test_uncommitted_acceptance_is_invisible_to_independent_claim(broadcast_db: PersistenceDB):
    [source] = await enqueue(broadcast_db)
    now = source.created_at + timedelta(seconds=1)
    assert await outbox.fail((await outbox.claim_batch(limit=1, lease_seconds=30, now=now))[0], error="done", now=now)
    item = NewBroadcastEmail(source.broadcast_id, source.tenant_id, source.id, "new@example.test", "New", "Body")

    async def rolled_back_acceptance() -> None:
        async with connection_ctx():
            _ = await outbox.enqueue_many([item])
            assert await outbox.claim_batch(limit=10, lease_seconds=30, now=now) == []
            raise RuntimeError("rollback")

    with pytest.raises(RuntimeError, match="rollback"):
        await rolled_back_acceptance()


async def test_tenant_deletion_cascades_recipient_payloads(broadcast_db: PersistenceDB):
    [original] = await enqueue(broadcast_db)
    await broadcast_db.admin.execute("DELETE FROM tenants WHERE id = %(id)s", {"id": original.tenant_id})
    assert await outbox.claim_batch(limit=1, lease_seconds=30, now=original.created_at + timedelta(seconds=1)) == []
