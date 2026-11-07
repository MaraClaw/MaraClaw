from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from pydantic import TypeAdapter

from app.api import notification as notification_api
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.dao.broadcast_submission_dao import broadcast_submission_dao as submissions
from app.db.session import bind_crud_connection
from app.records.user import UserRecord
from app.schemas.notification_responses import BroadcastHistoryCursor, NotificationBroadcastHistoryPageResponse
from broadcast_email_persistence_fixtures import PersistenceDB, broadcast_db, member, tenant

__all__ = ["broadcast_db"]

HISTORY_PATH = "/api/notifications/broadcast/history"
_PAGE = TypeAdapter(NotificationBroadcastHistoryPageResponse)
PUBLIC_FIELDS = {
    "broadcast_id",
    "sender_id",
    "sender_name",
    "title",
    "body",
    "send_email",
    "published_at",
    "users_notified",
    "agents_notified",
    "emails_sent",
}


async def _admin(db: PersistenceDB, tenant_id: UUID, recipients: int = 0) -> UserRecord:
    sender = await member(db, tenant_id)
    sender.role = UserRole.ORG_ADMIN
    for _ in range(recipients):
        _ = await member(db, tenant_id)
    return sender


async def _send(sender: UserRecord, key: str | None = None, **fields: object) -> dict[str, object]:
    request = notification_api.BroadcastRequest.model_validate({"title": "Update", "body": "Body"} | fields)
    return dict(await notification_api.broadcast_notification(request, current_user=sender, idempotency_key=key))


async def _history_count(db: PersistenceDB) -> int:
    return int(str(await db.admin.fetchval("SELECT count(*) FROM broadcast_history")))


async def _get_page(client: httpx.AsyncClient, **params: str | int) -> NotificationBroadcastHistoryPageResponse:
    response = await client.get(HISTORY_PATH, params=params)
    assert response.status_code == 200
    return _PAGE.validate_json(response.content)


@asynccontextmanager
async def _client(sender: UserRecord) -> AsyncGenerator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(notification_api.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
    app.dependency_overrides[get_current_user] = lambda: sender
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def _seed(
    db: PersistenceDB, tenant_id: UUID, title: str, published_at: datetime, *, id_: UUID | None = None
) -> UUID:
    broadcast_id = id_ or uuid4()
    await db.admin.execute(
        """INSERT INTO broadcast_history
           (broadcast_id, tenant_id, sender_name, title, body, send_email, users_notified, agents_notified,
            emails_sent, published_at)
           VALUES (%(id)s, %(tenant)s, 'Seed', %(title)s, '', false, 0, 0, 0, %(at)s)""",
        {"id": broadcast_id, "tenant": tenant_id, "title": title, "at": published_at},
    )
    return broadcast_id


@pytest.mark.parametrize("key", [None, "draft-1"])
async def test_committed_broadcast_writes_exactly_one_attributed_history_row(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch, key: str | None
) -> None:
    # Given an admin with two recipients and configured SMTP.
    tenant_id = await tenant(broadcast_db)
    sender = await _admin(broadcast_db, tenant_id, recipients=2)
    monkeypatch.setattr(
        "app.services.system_email_service.resolve_email_config_async", AsyncMock(return_value=object())
    )
    # When the broadcast commits, keyed or not.
    sent = await _send(sender, key, title="Maintenance", body="Tonight", send_email=True)
    # Then one row carries the committed content, attribution and counts.
    rows = await broadcast_db.admin.fetchall("SELECT * FROM broadcast_history")
    assert len(rows) == 1
    [row] = rows
    assert row["broadcast_id"] == sent["broadcast_id"]
    assert (row["tenant_id"], row["sender_id"], row["sender_name"]) == (tenant_id, sender.id, "Alice")
    assert (row["title"], row["body"], row["send_email"]) == ("Maintenance", "Tonight", True)
    assert (row["users_notified"], row["agents_notified"], row["emails_sent"]) == (2, 0, 2)
    assert row["published_at"] is not None
    assert not {"idempotency_key", "fingerprint"} & set(row)


async def test_zero_recipient_broadcast_is_still_published(broadcast_db: PersistenceDB) -> None:
    tenant_id = await tenant(broadcast_db)
    sender = await _admin(broadcast_db, tenant_id)
    sent = await _send(sender)
    row = await broadcast_db.admin.fetchone("SELECT * FROM broadcast_history")
    assert row is not None
    assert row["broadcast_id"] == sent["broadcast_id"]
    assert (row["users_notified"], row["agents_notified"], row["emails_sent"]) == (0, 0, 0)


async def test_replay_and_conflict_do_not_duplicate_history(broadcast_db: PersistenceDB) -> None:
    tenant_id = await tenant(broadcast_db)
    sender = await _admin(broadcast_db, tenant_id, recipients=1)
    first = await _send(sender, "draft-1")
    assert await _send(sender, "draft-1") == first
    with pytest.raises(HTTPException) as conflict:
        _ = await _send(sender, "draft-1", title="Other")
    assert conflict.value.status_code == 409
    assert await broadcast_db.admin.fetchval("SELECT array_agg(broadcast_id) FROM broadcast_history") == [
        first["broadcast_id"]
    ]


@pytest.mark.parametrize("key", [None, "draft-1"])
async def test_fan_out_failure_leaves_no_history_and_key_stays_reusable(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch, key: str | None
) -> None:
    tenant_id = await tenant(broadcast_db)
    sender = await _admin(broadcast_db, tenant_id, recipients=1)
    with monkeypatch.context() as patch:
        patch.setattr(notification_api.agent_dao, "list_for_tenant", AsyncMock(side_effect=RuntimeError("fan-out")))
        with pytest.raises(RuntimeError, match="fan-out"):
            _ = await _send(sender, key)
    assert await _history_count(broadcast_db) == 0
    _ = await _send(sender, key)
    assert await _history_count(broadcast_db) == 1


async def test_failure_after_history_insert_rolls_it_back(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = await tenant(broadcast_db)
    sender = await _admin(broadcast_db, tenant_id, recipients=1)
    monkeypatch.setattr(submissions, "record_counts", AsyncMock(side_effect=RuntimeError("late failure")))
    with pytest.raises(RuntimeError, match="late failure"):
        _ = await _send(sender, "draft-1")
    assert await _history_count(broadcast_db) == 0
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM notifications") == 0


async def test_smtp_refusal_writes_no_history(broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = await tenant(broadcast_db)
    sender = await _admin(broadcast_db, tenant_id, recipients=1)
    monkeypatch.setattr("app.services.system_email_service.resolve_email_config_async", AsyncMock(return_value=None))
    for key in (None, "draft-1"):
        with pytest.raises(HTTPException) as refused:
            _ = await _send(sender, key, send_email=True)
        assert refused.value.status_code == 400
    assert await _history_count(broadcast_db) == 0


async def test_history_outlives_sender_removal_and_leaves_with_tenant(broadcast_db: PersistenceDB) -> None:
    tenant_id = await tenant(broadcast_db)
    sender = await _admin(broadcast_db, tenant_id)
    _ = await _send(sender)
    await broadcast_db.admin.execute("DELETE FROM users WHERE id = %(id)s", {"id": sender.id})
    row = await broadcast_db.admin.fetchone("SELECT sender_id, sender_name FROM broadcast_history")
    assert row == {"sender_id": None, "sender_name": "Alice"}
    await broadcast_db.admin.execute("DELETE FROM tenants WHERE id = %(id)s", {"id": tenant_id})
    assert await _history_count(broadcast_db) == 0


async def test_sent_broadcast_is_read_back_through_the_route(broadcast_db: PersistenceDB) -> None:
    tenant_id = await tenant(broadcast_db)
    sender = await _admin(broadcast_db, tenant_id, recipients=1)
    sent = await _send(sender, "draft-1", title="Hello", body="World")
    async with _client(sender) as client:
        response = await client.get(HISTORY_PATH)
    assert response.status_code == 200
    page = response.json()
    assert page["next_cursor"] is None
    [item] = page["items"]
    assert set(item) == PUBLIC_FIELDS
    assert item["broadcast_id"] == str(sent["broadcast_id"])
    assert (item["sender_id"], item["sender_name"]) == (str(sender.id), "Alice")
    assert (item["title"], item["body"], item["send_email"]) == ("Hello", "World", False)
    assert (item["users_notified"], item["agents_notified"], item["emails_sent"]) == (1, 0, 0)
    assert datetime.fromisoformat(item["published_at"]).tzinfo is not None


async def test_history_is_tenant_scoped_latest_first_and_cursor_stable(broadcast_db: PersistenceDB) -> None:
    # Given two tenants, with ties on the timestamp broken by broadcast id.
    mine, other = await tenant(broadcast_db), await tenant(broadcast_db)
    admin, outsider = await _admin(broadcast_db, mine), await _admin(broadcast_db, other)
    base = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    tie = base + timedelta(minutes=2)
    ids = sorted([uuid4(), uuid4()])
    expected = [
        await _seed(broadcast_db, mine, "tie-high", tie, id_=ids[1]),
        await _seed(broadcast_db, mine, "tie-low", tie, id_=ids[0]),
        await _seed(broadcast_db, mine, "newest-first", base + timedelta(minutes=3)),
        await _seed(broadcast_db, mine, "oldest", base),
        await _seed(broadcast_db, mine, "middle", base + timedelta(minutes=1)),
    ]
    expected = [expected[2], expected[0], expected[1], expected[4], expected[3]]
    foreign = await _seed(broadcast_db, other, "foreign", base + timedelta(minutes=10))
    # When an admin pages two at a time and a newer broadcast lands mid-traversal.
    seen: list[UUID] = []
    async with _client(admin) as client:
        cursor: str | None = None
        for page_number in range(10):
            page = await _get_page(client, limit=2, **({} if cursor is None else {"cursor": cursor}))
            seen.extend(item["broadcast_id"] for item in page["items"])
            cursor = page["next_cursor"]
            if page_number == 0:
                _ = await _seed(broadcast_db, mine, "late", base + timedelta(hours=1))
            if cursor is None:
                break
        # Then the traversal is complete, ordered, free of duplicates and of the other tenant.
        assert seen == expected
        assert foreign not in seen
        # And a cursor minted for one tenant yields only the redeeming admin's own rows.
        async with _client(outsider) as other_client:
            first = await _get_page(client, limit=1)
            assert first["next_cursor"] is not None
            leaked = await _get_page(other_client, cursor=first["next_cursor"])
    assert [item["broadcast_id"] for item in leaked["items"]] == [foreign]


async def test_cursor_edges_exact_page_and_past_the_end(broadcast_db: PersistenceDB) -> None:
    tenant_id = await tenant(broadcast_db)
    admin = await _admin(broadcast_db, tenant_id)
    base = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    for minute in range(3):
        _ = await _seed(broadcast_db, tenant_id, f"b{minute}", base + timedelta(minutes=minute))
    async with _client(admin) as client:
        exact = await _get_page(client, limit=3)
        assert len(exact["items"]) == 3
        assert exact["next_cursor"] is None
        partial = await _get_page(client, limit=2)
        assert partial["next_cursor"] is not None
        tail = await _get_page(client, limit=2, cursor=partial["next_cursor"])
        assert [item["title"] for item in tail["items"]] == ["b0"]
        assert tail["next_cursor"] is None
        oldest = exact["items"][-1]
        after_oldest = BroadcastHistoryCursor(
            version=1,
            published_at=oldest["published_at"],
            broadcast_id=oldest["broadcast_id"],
        )
        past = await _get_page(client, cursor=after_oldest.encode())
        assert past == {"items": [], "next_cursor": None}
        assert (await client.get(HISTORY_PATH, params={"cursor": "garbage"})).status_code == 400
        empty = await _admin(broadcast_db, await tenant(broadcast_db))
    async with _client(empty) as client:
        assert await _get_page(client) == {"items": [], "next_cursor": None}
