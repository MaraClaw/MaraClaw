from base64 import urlsafe_b64encode
from datetime import timedelta
from uuid import UUID

import httpx
import pytest

from app.core.notification_kinds import NotificationKind
from app.records.notification import NotificationRecord
from notification_db_fakes import NotificationConnection, notification_db
from test_notification_filters import MEMBER, STAMP, client, populated_db

__all__ = ["client", "notification_db", "populated_db"]
PAGE = "/api/notifications/page"
pytestmark = pytest.mark.usefixtures("notification_db")


async def test_cursor_traverses_ties_without_shifting_after_newer_inserts(
    client: httpx.AsyncClient, populated_db: NotificationConnection
) -> None:
    # Given: tied broadcast rows and a first page ending within the tie.
    first = await client.get(PAGE, params={"category": "broadcast", "unread_only": "true", "limit": 20})
    assert first.status_code == 200
    pages = [first.json()]
    populated_db.notifications.extend(
        NotificationRecord(
            id=UUID(int=index),
            user_id=MEMBER.id,
            type=NotificationKind.BROADCAST,
            title="New",
            created_at=STAMP + timedelta(seconds=index - 100),
        )
        for index in (100, 101)
    )
    # When: each continuation uses only the returned cursor.
    while pages[-1]["next_cursor"] is not None:
        response = await client.get(
            PAGE,
            params={
                "category": "broadcast",
                "unread_only": "true",
                "limit": 20,
                "cursor": pages[-1]["next_cursor"],
            },
        )
        assert response.status_code == 200
        pages.append(response.json())
    # Then: every original older row appears once, with no newly inserted head rows.
    assert [row["id"] for page in pages for row in page["items"]] == [
        str(UUID(int=index)) for index in range(64, 9, -1)
    ]
    assert [len(page["items"]) for page in pages] == [20, 20, 15]


@pytest.mark.parametrize("limit", [1, 200])
async def test_page_accepts_size_boundaries(client: httpx.AsyncClient, limit: int) -> None:
    # Given / When: an empty inbox at either supported page-size boundary.
    response = await client.get(PAGE, params={"limit": limit})
    # Then: empty pages are terminal, including the first page.
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize("limit", [0, -1, 201])
async def test_page_rejects_unsupported_sizes(client: httpx.AsyncClient, limit: int) -> None:
    # Given / When
    response = await client.get(PAGE, params={"limit": limit})
    # Then
    assert response.status_code == 422


@pytest.mark.parametrize("cursor", ["", "!", "a", "e30=", "☃", "a" * 1025])
async def test_page_rejects_malformed_encoding_before_query(
    client: httpx.AsyncClient, notification_db: NotificationConnection, cursor: str
) -> None:
    # Given / When
    response = await client.get(PAGE, params={"cursor": cursor})
    # Then
    assert response.status_code == 400
    assert notification_db.executed == []


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        b"{}",
        b'{"created_at":null,"id":null}',
        b'{"created_at":"2026-10-23T00:00:00Z","id":"00000000-0000-0000-0000-000000000001"}',
        b'{"version":1,"created_at":"2026-10-23","id":"00000000-0000-0000-0000-000000000001"}',
        b'{"version":1,"created_at":123,"id":"00000000-0000-0000-0000-000000000001"}',
        b'{"version":1,"created_at":"2026-10-23T00:00:00Z","id":"bad"}',
        b'{"version":true,"created_at":"2026-10-23T00:00:00Z","id":"00000000-0000-0000-0000-000000000001"}',
        b'{"version":2,"created_at":"2026-10-23T00:00:00Z","id":"00000000-0000-0000-0000-000000000001"}',
        b'{"version":1,"created_at":"2026-10-23T00:00:00Z","id":"00000000-0000-0000-0000-000000000001","offset":5}',
    ],
)
async def test_page_rejects_malformed_cursor_contents_before_query(
    client: httpx.AsyncClient, notification_db: NotificationConnection, payload: bytes
) -> None:
    # Given
    cursor = urlsafe_b64encode(payload).decode().rstrip("=")
    # When
    response = await client.get(PAGE, params={"cursor": cursor})
    # Then
    assert response.status_code == 400
    assert notification_db.executed == []


@pytest.mark.parametrize(("query", "status"), [({"view": "all"}, 422), ({"category": "missing"}, 400)])
async def test_page_rejects_invalid_filters(
    client: httpx.AsyncClient, notification_db: NotificationConnection, query: dict[str, str], status: int
) -> None:
    # Given / When
    response = await client.get(PAGE, params=query)
    # Then
    assert response.status_code == status
    assert notification_db.executed == []
