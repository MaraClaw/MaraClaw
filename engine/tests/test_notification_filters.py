from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import TypeAdapter

from app.api import notification as notification_api
from app.core.security import get_current_user
from app.records.notification import NotificationRecord
from app.records.user import UserRecord
from notification_db_fakes import NotificationConnection, notification_db as notification_db

pytestmark = pytest.mark.usefixtures("notification_db")
MEMBER = UserRecord(id=UUID(int=100), tenant_id=UUID(int=200))
FOREIGN_USER = UUID(int=101)
STAMP = datetime(2026, 10, 23, tzinfo=UTC)
ROWS = TypeAdapter(list[dict[str, object]])
FILTERS: list[tuple[str | None, list[str] | None]] = [
    (None, None),
    ("all", None),
    ("tool", ["autonomy_l2"]),
    ("approval", ["approval_pending", "approval_resolved", "company_admission_pending", "company_admission_resolved"]),
    ("social", ["plaza_comment", "plaza_reply", "mention", "broadcast"]),
    ("broadcast", ["broadcast"]),
]


@pytest.fixture
def populated_db(notification_db: NotificationConnection) -> NotificationConnection:
    types = [
        "autonomy_l2",
        "approval_pending",
        "approval_resolved",
        "company_admission_pending",
        "company_admission_resolved",
        "plaza_comment",
        "plaza_reply",
        "mention",
        "future_type",
        *(["broadcast"] * 55),
    ]
    notification_db.notifications = [
        NotificationRecord(id=UUID(int=index), user_id=MEMBER.id, type=kind, title=kind, created_at=STAMP)
        for index, kind in enumerate(types, start=1)
    ]
    notification_db.notifications.extend(
        [
            NotificationRecord(id=UUID(int=900), user_id=MEMBER.id, type="broadcast", title="read", is_read=True),
            NotificationRecord(id=UUID(int=901), user_id=FOREIGN_USER, type="broadcast", title="foreign"),
        ]
    )
    return notification_db


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(notification_api.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: MEMBER
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/notifications"),
        ("GET", "/api/notifications/unread-count"),
        ("POST", "/api/notifications/read-all"),
    ],
)
@pytest.mark.parametrize("category", ["", "unknown", "ALL", " social ", "autonomy_l2"])
async def test_invalid_categories_reject_before_dao_calls(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    category: str,
) -> None:
    # Given
    dao_calls = [AsyncMock() for _ in range(3)]
    for name, call in zip(("list_for_user", "count_unread_for_user", "mark_all_read_for_user"), dao_calls, strict=True):
        monkeypatch.setattr(notification_api.notification_dao, name, call)
    # When
    response = await client.request(method, path, params={"category": category})
    # Then
    assert response.status_code == 400
    for call in dao_calls:
        call.assert_not_called()


@pytest.mark.parametrize("view", ["", "all", "ACTIVE", " archived ", "unknown"])
async def test_invalid_views_reject_before_dao_calls(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, view: str
) -> None:
    # Given
    listing = AsyncMock()
    monkeypatch.setattr(notification_api.notification_dao, "list_for_user", listing)
    # When
    response = await client.get("/api/notifications", params={"view": view})
    # Then
    assert response.status_code == 422
    listing.assert_not_called()


@pytest.mark.parametrize(("category", "types"), FILTERS)
async def test_list_uses_validated_category_and_current_user(
    client: httpx.AsyncClient,
    populated_db: NotificationConnection,
    category: str | None,
    types: list[str] | None,
) -> None:
    # Given
    params: dict[str, str | int] = {"limit": 200, "unread_only": "true", "user_id": str(FOREIGN_USER)}
    if category is not None:
        params["category"] = category
    expected = [
        row
        for row in populated_db.notifications
        if row.user_id == MEMBER.id and not row.is_read and (types is None or row.type in types)
    ]
    # When
    response = await client.get("/api/notifications", params=params)
    # Then
    assert response.status_code == 200
    assert {row["id"] for row in ROWS.validate_json(response.content)} == {str(row.id) for row in expected}


@pytest.mark.parametrize(("category", "types"), FILTERS)
async def test_count_uses_same_category_and_current_user(
    client: httpx.AsyncClient,
    populated_db: NotificationConnection,
    category: str | None,
    types: list[str] | None,
) -> None:
    # Given
    params = {} if category is None else {"category": category}
    expected = sum(
        row.user_id == MEMBER.id and not row.is_read and (types is None or row.type in types)
        for row in populated_db.notifications
    )
    # When
    response = await client.get("/api/notifications/unread-count", params=params)
    # Then
    assert response.status_code == 200
    assert response.json() == {"unread_count": expected}


@pytest.mark.parametrize(("category", "types"), FILTERS)
async def test_bulk_read_scopes_all_matching_rows_beyond_first_page(
    client: httpx.AsyncClient,
    populated_db: NotificationConnection,
    category: str | None,
    types: list[str] | None,
) -> None:
    # Given
    params = {} if category is None else {"category": category}
    expected_unread = {
        row.id
        for row in populated_db.notifications
        if not row.is_read and (row.user_id != MEMBER.id or (types is not None and row.type not in types))
    }
    # When
    response = await client.post("/api/notifications/read-all", params=params)
    # Then
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert {row.id for row in populated_db.notifications if not row.is_read} == expected_unread


@pytest.mark.parametrize(("category", "social_count"), [("social", 0), ("broadcast", 3)])
async def test_bulk_read_updates_overlapping_social_and_broadcast_counts(
    client: httpx.AsyncClient,
    populated_db: NotificationConnection,
    category: str,
    social_count: int,
) -> None:
    # Given / When
    response = await client.post("/api/notifications/read-all", params={"category": category})
    # Then
    assert response.status_code == 200
    social = await client.get("/api/notifications/unread-count", params={"category": "social"})
    broadcast = await client.get("/api/notifications/unread-count", params={"category": "broadcast"})
    assert social.json() == {"unread_count": social_count}
    assert broadcast.json() == {"unread_count": 0}


async def test_list_pages_have_stable_id_tiebreaker(
    client: httpx.AsyncClient, populated_db: NotificationConnection
) -> None:
    # Given / When
    pages = [
        await client.get(
            "/api/notifications", params={"category": "broadcast", "unread_only": "true", "limit": 50, "offset": offset}
        )
        for offset in (0, 50)
    ]
    # Then
    assert all(page.status_code == 200 for page in pages)
    assert [row["id"] for page in pages for row in ROWS.validate_json(page.content)] == [
        str(UUID(int=index)) for index in range(64, 9, -1)
    ]


async def test_direct_bulk_read_preserves_positional_user_call(populated_db: NotificationConnection) -> None:
    # Given / When
    result = await notification_api.mark_all_read(MEMBER)
    # Then
    assert result == {"ok": True}
    assert all(row.is_read for row in populated_db.notifications if row.user_id == MEMBER.id)
