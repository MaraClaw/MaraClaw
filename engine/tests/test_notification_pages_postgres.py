from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import pytest
from pydantic import TypeAdapter

from app.schemas.notification_responses import NotificationPageResponse
from notification_archive_fixtures import ArchiveScenario, archive_scenario, schema_db

__all__ = ["archive_scenario", "schema_db"]
PAGE = "/api/notifications/page"
PAGE_RESPONSE = TypeAdapter(NotificationPageResponse)
STAMP = datetime(2026, 10, 23, 12, 30, 0, 123456, tzinfo=UTC)


@pytest.mark.parametrize("view", ["active", "archived"])
async def test_seek_keeps_older_rows_stable_across_inserts_with_timestamp_ties(
    archive_scenario: ArchiveScenario, view: Literal["active", "archived"]
) -> None:
    # Given: two timestamp groups, whose IDs run in the opposite order to their times.
    scenario = archive_scenario
    rows = [
        {"id": UUID(int=index), "stamp": STAMP if index <= 4 else STAMP - timedelta(seconds=1)} for index in range(1, 8)
    ]
    await scenario.db.executemany(
        "INSERT INTO notifications (id, user_id, type, title, body, is_read, created_at, archived_at) "
        "VALUES (%(id)s, %(owner)s, 'broadcast', 'Notice', '', FALSE, %(stamp)s, %(archive)s)",
        [{**row, "owner": scenario.owner.id, "archive": STAMP if view == "archived" else None} for row in rows],
    )
    params = {"view": view, "limit": "2", "category": "social"}
    headers = scenario.headers(scenario.owner)
    first = await scenario.client.get(PAGE, params=params, headers=headers)
    assert first.status_code == 200
    pages = [PAGE_RESPONSE.validate_json(first.content)]
    await scenario.db.executemany(
        "INSERT INTO notifications (id, user_id, type, title, body, is_read, created_at, archived_at) "
        "VALUES (%(id)s, %(owner)s, 'broadcast', 'Inserted', '', FALSE, %(stamp)s, %(archive)s)",
        [
            {
                "id": UUID(int=index),
                "owner": scenario.owner.id,
                "stamp": stamp,
                "archive": STAMP if view == "archived" else None,
            }
            for index, stamp in [(100, STAMP), (101, STAMP + timedelta(seconds=1))]
        ],
    )
    # When: older pages are traversed after newer inserts commit.
    while (cursor := pages[-1]["next_cursor"]) is not None:
        response = await scenario.client.get(PAGE, params={**params, "cursor": cursor}, headers=headers)
        assert response.status_code == 200
        pages.append(PAGE_RESPONSE.validate_json(response.content))
    # Then: all original rows appear once, ordered by both time and ID, without shifted boundaries.
    assert [row["id"] for page in pages for row in page["items"]] == [
        str(UUID(int=index)) for index in [4, 3, 2, 1, 7, 6, 5]
    ]
    assert [len(page["items"]) for page in pages] == [2, 2, 2, 1]


@pytest.mark.parametrize("mutation", ["archive", "read"])
async def test_continuation_can_be_empty_when_live_membership_changes(
    archive_scenario: ArchiveScenario, mutation: str
) -> None:
    # Given: a continuation for an unread row which is about to leave the active unread list.
    scenario = archive_scenario
    older = await scenario.notice()
    await scenario.db.execute(
        "UPDATE notifications SET created_at = %(stamp)s WHERE id = %(id)s", {"stamp": STAMP, "id": older.id}
    )
    await scenario.notice()
    headers = scenario.headers(scenario.owner)
    params = {"limit": "1", "unread_only": "true"}
    first = await scenario.client.get(PAGE, params=params, headers=headers)
    cursor = PAGE_RESPONSE.validate_json(first.content)["next_cursor"]
    assert cursor is not None
    changed = await scenario.client.post(f"/api/notifications/{older.id}/{mutation}", headers=headers)
    assert changed.status_code == 200
    # When: the continuation is fetched after the membership-changing mutation commits.
    final = await scenario.client.get(PAGE, params={**params, "cursor": cursor}, headers=headers)
    # Then: the live terminal page is empty rather than retaining a snapshot of the old membership.
    assert final.status_code == 200
    assert final.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize("count", [0, 1, 2, 3])
async def test_next_cursor_requires_a_matching_lookahead_row(archive_scenario: ArchiveScenario, count: int) -> None:
    # Given
    scenario = archive_scenario
    for _index in range(count):
        await scenario.notice()
    # When
    response = await scenario.client.get(PAGE, params={"limit": 2}, headers=scenario.headers(scenario.owner))
    # Then: a full final page has no next cursor unless an additional row actually matches.
    assert response.status_code == 200
    page = PAGE_RESPONSE.validate_json(response.content)
    assert len(page["items"]) == min(count, 2)
    assert (page["next_cursor"] is not None) is (count > 2)


@pytest.mark.parametrize("view", ["active", "archived"])
@pytest.mark.parametrize("unread_only", [False, True])
@pytest.mark.parametrize(
    ("category", "expected_kinds"),
    [
        ("all", {"autonomy_l2", "approval_pending", "mention", "broadcast"}),
        ("tool", {"autonomy_l2"}),
        ("approval", {"approval_pending"}),
        ("social", {"mention", "broadcast"}),
        ("broadcast", {"broadcast"}),
    ],
)
async def test_every_page_applies_ownership_view_read_and_category_filters(
    archive_scenario: ArchiveScenario,
    view: Literal["active", "archived"],
    unread_only: bool,
    category: str,
    expected_kinds: set[str],
) -> None:
    # Given: mixed recipient, read-state, category and view rows interleaved at a tied timestamp.
    scenario = archive_scenario
    seeds = [
        {"user_id": owner.id, "type": kind, "is_read": is_read, "archive": STAMP if archived else None}
        for owner in (scenario.owner, scenario.colleague, scenario.outsider)
        for kind in ("autonomy_l2", "approval_pending", "mention", "broadcast")
        for is_read in (False, True)
        for archived in (False, True)
    ]
    await scenario.db.executemany(
        "INSERT INTO notifications (id, user_id, type, title, body, is_read, created_at, archived_at) "
        "VALUES (%(id)s, %(user_id)s, %(type)s, 'Mixed', '', %(is_read)s, %(stamp)s, %(archive)s)",
        [{**seed, "id": UUID(int=index), "stamp": STAMP} for index, seed in enumerate(seeds, start=1)],
    )
    pages: list[NotificationPageResponse] = []
    params = {"view": view, "category": category, "unread_only": str(unread_only).lower(), "limit": "1"}
    cursor = None
    # When: every page is consumed, including boundaries next to other recipients and categories.
    while True:
        response = await scenario.client.get(
            PAGE,
            params={**params, **({"cursor": cursor} if cursor is not None else {})},
            headers=scenario.headers(scenario.owner),
        )
        assert response.status_code == 200
        pages.append(PAGE_RESPONSE.validate_json(response.content))
        cursor = pages[-1]["next_cursor"]
        if cursor is None:
            break
    # Then: only current matching membership appears, without duplication or skipped matching rows.
    expected = [
        str(UUID(int=index))
        for index, seed in reversed(list(enumerate(seeds, start=1)))
        if seed["user_id"] == scenario.owner.id
        and seed["type"] in expected_kinds
        and (seed["archive"] is not None) == (view == "archived")
        and (not unread_only or seed["is_read"] is False)
    ]
    assert [row["id"] for page in pages for row in page["items"]] == expected
    assert pages[-1]["next_cursor"] is None
