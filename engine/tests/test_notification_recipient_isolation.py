"""Exercise the real notification router/DAO with an in-memory SQL-semantic connection."""

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import final, override
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from pydantic import TypeAdapter

from app.api import notification as notification_api
from app.core.notification_kinds import NotificationKind
from app.core.security import get_current_user
from app.db.connection import Params
from app.records.notification import NotificationRecord
from app.records.user import UserRecord
from app.schemas.notification_responses import NotificationResponse
from notification_db_fakes import NotificationConnection, _Cursor, notification_db

__all__ = ["notification_db"]

OWNER = UserRecord(id=UUID(int=100), identity_id=UUID(int=1000), tenant_id=UUID(int=10))
SAME_TENANT = UserRecord(id=UUID(int=200), identity_id=UUID(int=2000), tenant_id=OWNER.tenant_id)
FOREIGN_TENANT = UserRecord(id=UUID(int=300), identity_id=UUID(int=3000), tenant_id=UUID(int=20))
ACCOUNTS = {user.id: user for user in (OWNER, SAME_TENANT, FOREIGN_TENANT)}
PRIVATE_ID = UUID(int=1)
OWNER_SIBLING_ID = UUID(int=2)
OWNER_READ_ID = UUID(int=3)
ROWS = TypeAdapter(list[NotificationResponse])
RECIPIENTS = pytest.mark.parametrize("actor", [OWNER, SAME_TENANT, FOREIGN_TENANT], ids=["owner", "same", "foreign"])
OTHER_RECIPIENTS = pytest.mark.parametrize("actor", [SAME_TENANT, FOREIGN_TENANT], ids=["same", "foreign"])


@final
class _RecipientCursor(_Cursor):
    """Add the ID predicate missing from the shared filter-only cursor fake."""

    @override
    async def execute(self, query: str, params: Params = None) -> None:
        sql = " ".join(query.split())
        if "id = ANY(%(ids)s)" in sql:
            assert isinstance(params, Mapping)
            self.raw.executed.append((sql, dict(params)))
            ids = params["ids"]
            assert isinstance(ids, list)
            matching = [row for row in self.raw.notifications if row.id in ids and row.user_id == params["user_id"]]
            if sql.startswith("SELECT "):
                self.rows = [{"id": row.id} for row in matching]
            else:
                for row in matching:
                    row.is_read = True
            return
        if sql.startswith("UPDATE notifications SET archived_at ="):
            assert isinstance(params, Mapping)
            self.raw.executed.append((sql, dict(params)))
            for row in self.raw.notifications:
                if row.id == params["id"] and row.user_id == params["user_id"]:
                    row.archived_at = (
                        None
                        if "SET archived_at = NULL" in sql
                        else row.archived_at or datetime(2026, 10, 24, tzinfo=UTC)
                    )
                    self.rows = [{"id": row.id}]
            return
        if not (sql.startswith("UPDATE notifications SET is_read = TRUE") and "id = %(id)s" in sql):
            await super().execute(query, params)
            return
        assert isinstance(params, Mapping)
        self.raw.executed.append((sql, dict(params)))
        for row in self.raw.notifications:
            if row.id == params["id"] and ("user_id = %(user_id)s" not in sql or row.user_id == params["user_id"]):
                row.is_read = True


@pytest.fixture
def populated_db(notification_db: NotificationConnection, monkeypatch: pytest.MonkeyPatch) -> NotificationConnection:
    monkeypatch.setattr(notification_db, "cursor", lambda: _RecipientCursor(notification_db))
    notification_db.notifications = [
        NotificationRecord(
            id=note_id,
            user_id=user.id,
            type=NotificationKind.ORGANIZATION_WELCOME,
            title="Private welcome",
            is_read=read,
        )
        for note_id, user, read in [
            (PRIVATE_ID, OWNER, False),
            (OWNER_SIBLING_ID, OWNER, False),
            (OWNER_READ_ID, OWNER, True),
            (UUID(int=4), SAME_TENANT, False),
            (UUID(int=5), FOREIGN_TENANT, False),
        ]
    ]
    return notification_db


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(notification_api.router, prefix="/api")

    def current_user(request: Request) -> UserRecord:
        return ACCOUNTS[UUID(request.headers["x-test-user"])]

    app.dependency_overrides[get_current_user] = current_user
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@RECIPIENTS
async def test_list_contains_only_membership_recipient_notifications(
    client: httpx.AsyncClient, populated_db: NotificationConnection, actor: UserRecord
) -> None:
    # Given: every account has private notifications, with identity IDs distinct from membership IDs.
    expected = {str(row.id) for row in populated_db.notifications if row.user_id == actor.id}
    # When: a query parameter attempts to select the private notification's owner.
    response = await client.get(
        "/api/notifications", headers={"x-test-user": str(actor.id)}, params={"user_id": str(OWNER.id)}
    )
    # Then: even a same-tenant account sees only its own rows, never the owner's private row.
    assert response.status_code == 200
    assert {row["id"] for row in ROWS.validate_json(response.content)} == expected


@RECIPIENTS
async def test_unread_count_contains_only_membership_recipient_notifications(
    client: httpx.AsyncClient, populated_db: NotificationConnection, actor: UserRecord
) -> None:
    # Given
    expected = sum(row.user_id == actor.id and not row.is_read for row in populated_db.notifications)
    # When
    response = await client.get("/api/notifications/unread-count", headers={"x-test-user": str(actor.id)})
    # Then
    assert response.status_code == 200
    assert response.json() == {"unread_count": expected}


@OTHER_RECIPIENTS
async def test_read_leaves_another_accounts_private_notification_unchanged(
    client: httpx.AsyncClient, populated_db: NotificationConnection, actor: UserRecord
) -> None:
    # Given
    before = {row.id: row.is_read for row in populated_db.notifications}
    # When
    response = await client.post(f"/api/notifications/{PRIVATE_ID}/read", headers={"x-test-user": str(actor.id)})
    # Then: a successful no-op must not change the owner or any of the caller's rows.
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert {row.id: row.is_read for row in populated_db.notifications} == before


@OTHER_RECIPIENTS
async def test_read_all_changes_only_the_callers_private_notifications(
    client: httpx.AsyncClient, populated_db: NotificationConnection, actor: UserRecord
) -> None:
    # Given
    expected_read = {OWNER_READ_ID} | {row.id for row in populated_db.notifications if row.user_id == actor.id}
    # When
    response = await client.post("/api/notifications/read-all", headers={"x-test-user": str(actor.id)})
    # Then: the caller's own row changes, but the private owner rows stay unread.
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert {row.id for row in populated_db.notifications if row.is_read} == expected_read


async def test_owner_can_read_exactly_one_private_notification(
    client: httpx.AsyncClient, populated_db: NotificationConnection
) -> None:
    # Given: the owner has two unread notifications, so an ID-less update would be observable.
    # When
    response = await client.post(f"/api/notifications/{PRIVATE_ID}/read", headers={"x-test-user": str(OWNER.id)})
    # Then
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert {row.id for row in populated_db.notifications if row.is_read} == {PRIVATE_ID, OWNER_READ_ID}


async def test_owner_can_read_all_private_notifications_without_changing_other_accounts(
    client: httpx.AsyncClient, populated_db: NotificationConnection
) -> None:
    # Given
    # When
    response = await client.post("/api/notifications/read-all", headers={"x-test-user": str(OWNER.id)})
    # Then
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert {row.id for row in populated_db.notifications if row.is_read} == {
        PRIVATE_ID,
        OWNER_SIBLING_ID,
        OWNER_READ_ID,
    }


@OTHER_RECIPIENTS
@pytest.mark.parametrize("operation", ["archive", "restore"])
async def test_archive_mutations_hide_foreign_ownership(
    client: httpx.AsyncClient, populated_db: NotificationConnection, actor: UserRecord, operation: str
) -> None:
    # Given
    before = [(row.id, row.is_read, row.archived_at) for row in populated_db.notifications]
    # When
    foreign = await client.post(f"/api/notifications/{PRIVATE_ID}/{operation}", headers={"x-test-user": str(actor.id)})
    missing = await client.post(
        f"/api/notifications/{UUID(int=999)}/{operation}", headers={"x-test-user": str(actor.id)}
    )
    # Then
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert [(row.id, row.is_read, row.archived_at) for row in populated_db.notifications] == before


@pytest.mark.parametrize("invalid_id", [UUID(int=4), UUID(int=5), UUID(int=999)], ids=["same", "foreign", "missing"])
async def test_selected_read_rejects_mixed_invalid_sets_without_writes(
    client: httpx.AsyncClient, populated_db: NotificationConnection, invalid_id: UUID
) -> None:
    # Given: a valid unread selection mixed with one inaccessible selector.
    before = [(row.id, row.is_read, row.archived_at) for row in populated_db.notifications]
    # When
    response = await client.post(
        "/api/notifications/read-selected",
        headers={"x-test-user": str(OWNER.id)},
        json={"ids": [str(PRIVATE_ID), str(invalid_id)]},
    )
    # Then
    assert response.status_code == 404
    assert response.json() == {"detail": "Notification not found"}
    assert [(row.id, row.is_read, row.archived_at) for row in populated_db.notifications] == before
    assert all(not query.startswith("UPDATE ") for query, _params in populated_db.executed)


@pytest.mark.parametrize("count", [1, 200])
async def test_selected_read_accepts_submitted_bounds_and_duplicate_ids(
    client: httpx.AsyncClient, populated_db: NotificationConnection, count: int
) -> None:
    # Given: duplicates of one owned row, with an unselected unread sibling.
    # When
    response = await client.post(
        "/api/notifications/read-selected",
        headers={"x-test-user": str(OWNER.id)},
        json={"ids": [str(PRIVATE_ID)] * count},
    )
    # Then
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert {row.id for row in populated_db.notifications if row.is_read} == {PRIVATE_ID, OWNER_READ_ID}


@pytest.mark.parametrize("ids", [[], [str(PRIVATE_ID)] * 201, ["not-a-uuid"], [None], None])
async def test_selected_read_validates_submitted_ids_before_database_access(
    client: httpx.AsyncClient, populated_db: NotificationConnection, ids: list[str | None] | None
) -> None:
    # Given / When: invalid UUIDs or submitted bounds, even when deduplication would fit.
    response = await client.post(
        "/api/notifications/read-selected", headers={"x-test-user": str(OWNER.id)}, json={"ids": ids}
    )
    # Then
    assert response.status_code == 422
    assert populated_db.executed == []


async def test_selected_read_requires_authentication() -> None:
    # Given: the real auth dependency, without the membership fixture override.
    app = FastAPI()
    app.include_router(notification_api.router, prefix="/api")
    # When
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/notifications/read-selected", json={"ids": [str(PRIVATE_ID)]})
    # Then
    assert response.status_code == 401
