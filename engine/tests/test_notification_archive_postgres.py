from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter

from app.core.notification_kinds import NotificationKind
from app.dao.notification_dao import notification_dao
from app.schemas.notification_responses import NotificationResponse
from notification_archive_fixtures import ArchiveScenario, archive_scenario, schema_db
from notification_read_selected_postgres import (
    test_selected_read_enforces_submitted_upper_bound_in_postgres,
    test_selected_read_rejects_hidden_rows_even_when_archived_or_read,
    test_selected_read_rejects_mixed_invalid_ids_atomically,
    test_selected_read_rolls_back_when_commit_fails,
)

__all__ = [
    "archive_scenario",
    "schema_db",
    "test_selected_read_enforces_submitted_upper_bound_in_postgres",
    "test_selected_read_rejects_hidden_rows_even_when_archived_or_read",
    "test_selected_read_rejects_mixed_invalid_ids_atomically",
    "test_selected_read_rolls_back_when_commit_fails",
]

ROWS = TypeAdapter(list[NotificationResponse])


@pytest.mark.parametrize("is_read", [False, True])
async def test_archive_is_persistent_idempotent_and_preserves_read_state(
    archive_scenario: ArchiveScenario, is_read: bool
) -> None:
    # Given a notice with an independently chosen read state.
    scenario = archive_scenario
    notice = await scenario.notice(is_read=is_read)
    headers = scenario.headers(scenario.owner)
    # When the owner archives and replays the operation in separate HTTP transactions.
    first = await scenario.client.post(f"/api/notifications/{notice.id}/archive", headers=headers)
    first_row = await scenario.db.fetchone("SELECT * FROM notifications WHERE id = %(id)s", {"id": notice.id})
    replay = await scenario.client.post(f"/api/notifications/{notice.id}/archive", headers=headers)
    second_row = await scenario.db.fetchone("SELECT * FROM notifications WHERE id = %(id)s", {"id": notice.id})
    # Then the committed timestamp survives replay, and read state is unchanged.
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json() == {"ok": True}
    assert first_row is not None
    assert second_row is not None
    assert isinstance(first_row["archived_at"], datetime)
    assert second_row["archived_at"] == first_row["archived_at"]
    assert second_row["is_read"] is is_read
    active = await scenario.client.get("/api/notifications", headers=headers)
    archived = await scenario.client.get("/api/notifications?view=archived", headers=headers)
    assert ROWS.validate_json(active.content) == []
    assert ROWS.validate_json(archived.content)[0]["archived_at"] == first_row["archived_at"].isoformat()
    for category in ("all", "social", "broadcast"):
        count = await scenario.client.get(f"/api/notifications/unread-count?category={category}", headers=headers)
        assert count.json() == {"unread_count": 0}


@pytest.mark.parametrize("operation", ["archive", "restore"])
@pytest.mark.parametrize("recipient", ["colleague", "outsider"])
async def test_foreign_and_missing_mutations_are_indistinguishable(
    archive_scenario: ArchiveScenario, operation: str, recipient: str
) -> None:
    # Given an owned row, including archived state when attempting restoration.
    scenario = archive_scenario
    notice = await scenario.notice()
    if operation == "restore":
        assert await notification_dao.archive_for_user(notice.id, scenario.owner.id)
    before = await notification_dao.get(notice.id)
    user = scenario.colleague if recipient == "colleague" else scenario.outsider
    # When a different membership supplies both a real id and a missing id.
    foreign = await scenario.client.post(f"/api/notifications/{notice.id}/{operation}", headers=scenario.headers(user))
    missing = await scenario.client.post(f"/api/notifications/{uuid4()}/{operation}", headers=scenario.headers(user))
    # Then neither ownership nor existence leaks, and the stored row is untouched.
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert await notification_dao.get(notice.id) == before


@pytest.mark.parametrize("is_read", [False, True])
async def test_restore_preserves_read_state_and_reenters_active_counts(
    archive_scenario: ArchiveScenario, is_read: bool
) -> None:
    # Given an archived row which active mark-all must not read.
    scenario = archive_scenario
    notice = await scenario.notice(is_read=is_read)
    await scenario.notice()
    headers = scenario.headers(scenario.owner)
    assert await notification_dao.archive_for_user(notice.id, scenario.owner.id)
    bulk = await scenario.client.post("/api/notifications/read-all?category=social", headers=headers)
    assert bulk.status_code == 200
    count = await scenario.client.get("/api/notifications/unread-count?category=broadcast", headers=headers)
    assert count.json() == {"unread_count": 0}
    # When the owner restores the same row twice.
    restored = await scenario.client.post(f"/api/notifications/{notice.id}/restore", headers=headers)
    replay = await scenario.client.post(f"/api/notifications/{notice.id}/restore", headers=headers)
    # Then the row remains active with its original read state and correct overlapping badges.
    assert restored.status_code == replay.status_code == 200
    assert restored.json() == replay.json() == {"ok": True}
    stored = await notification_dao.get(notice.id)
    assert stored is not None
    assert stored.archived_at is None
    assert stored.is_read is is_read
    for category in ("all", "social", "broadcast"):
        count = await scenario.client.get(f"/api/notifications/unread-count?category={category}", headers=headers)
        assert count.json() == {"unread_count": int(not is_read)}


async def test_individual_read_of_archived_notice_does_not_restore_it(archive_scenario: ArchiveScenario) -> None:
    # Given an archived unread notice.
    scenario = archive_scenario
    notice = await scenario.notice()
    assert await notification_dao.archive_for_user(notice.id, scenario.owner.id)
    before = await notification_dao.get(notice.id)
    assert before is not None
    # When the owner explicitly reads the archived notice.
    response = await scenario.client.post(
        f"/api/notifications/{notice.id}/read", headers=scenario.headers(scenario.owner)
    )
    # Then reading does not change its archive timestamp.
    assert response.status_code == 200
    stored = await notification_dao.get(notice.id)
    assert stored is not None
    assert stored.is_read is True
    assert stored.archived_at == before.archived_at


@pytest.mark.parametrize("unread_only", [False, True])
async def test_archived_category_overlap_and_equal_timestamp_pagination(
    archive_scenario: ArchiveScenario, unread_only: bool
) -> None:
    # Given more than one archived page, including overlapping types and foreign rows.
    scenario = archive_scenario
    stamp = datetime(2026, 10, 23, tzinfo=UTC)
    rows = [
        {"id": UUID(int=index), "user_id": scenario.owner.id, "type": "broadcast", "read": False}
        for index in range(1, 61)
    ] + [
        {"id": UUID(int=1002), "user_id": scenario.owner.id, "type": "mention", "read": False},
        {"id": UUID(int=1003), "user_id": scenario.owner.id, "type": "broadcast", "read": True},
        {"id": UUID(int=1004), "user_id": scenario.owner.id, "type": "autonomy_l2", "read": False},
        {"id": UUID(int=1005), "user_id": scenario.colleague.id, "type": "broadcast", "read": False},
    ]
    await scenario.db.executemany(
        "INSERT INTO notifications (id, user_id, type, title, body, is_read, created_at, archived_at) "
        "VALUES (%(id)s, %(user_id)s, %(type)s, 'Notice', '', %(read)s, %(stamp)s, %(stamp)s)",
        [{**row, "stamp": stamp} for row in rows],
    )
    active = await scenario.notice()
    headers = scenario.headers(scenario.owner)
    # When the archived social inbox is fetched as two pages.
    pages = [
        await scenario.client.get(
            "/api/notifications",
            params={
                "view": "archived",
                "category": "social",
                "unread_only": str(unread_only).lower(),
                "limit": 50,
                "offset": offset,
                "user_id": str(scenario.colleague.id),
            },
            headers=headers,
        )
        for offset in (0, 50)
    ]
    # Then ties are stable, category overlap survives, and active/foreign/tool rows stay out.
    assert all(page.status_code == 200 for page in pages)
    expected = ([1003] if not unread_only else []) + [1002, *range(60, 0, -1)]
    assert [row["id"] for page in pages for row in ROWS.validate_json(page.content)] == [
        str(UUID(int=value)) for value in expected
    ]
    active_page = await scenario.client.get("/api/notifications?view=active", headers=headers)
    assert [(row["id"], row["archived_at"]) for row in ROWS.validate_json(active_page.content)] == [
        (str(active.id), None)
    ]


async def test_bulk_read_covers_all_active_matches_without_reading_archived_or_other_categories(
    archive_scenario: ArchiveScenario,
) -> None:
    # Given matching active notices across two pages and unread notices outside the target set.
    scenario = archive_scenario
    active_ids = [UUID(int=index) for index in range(1, 61)]
    await scenario.db.executemany(
        "INSERT INTO notifications (id, user_id, type, title, body, is_read) "
        "VALUES (%(id)s, %(user_id)s, 'broadcast', 'Notice', '', FALSE)",
        [{"id": notice_id, "user_id": scenario.owner.id} for notice_id in active_ids],
    )
    archived = await scenario.notice()
    await notification_dao.archive_for_user(archived.id, scenario.owner.id)
    mention = await scenario.notice(kind=NotificationKind.MENTION)
    tool = await scenario.notice(kind=NotificationKind.AUTONOMY_L2)
    foreign = await notification_dao.create(
        obj_in={"user_id": scenario.colleague.id, "type": NotificationKind.BROADCAST.value, "title": "Foreign"}
    )
    # When all active broadcasts are marked read, without a page limit.
    response = await scenario.client.post(
        "/api/notifications/read-all?category=broadcast", headers=scenario.headers(scenario.owner)
    )
    # Then every match is read, while archived, other-category and foreign unread state is preserved.
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    stored = await scenario.db.fetchall("SELECT id, is_read FROM notifications")
    assert {row["id"]: row["is_read"] for row in stored} == {
        **dict.fromkeys(active_ids, True),
        archived.id: False,
        mention.id: False,
        tool.id: False,
        foreign.id: False,
    }


async def test_selected_read_is_idempotent_for_archived_and_already_read_notices(
    archive_scenario: ArchiveScenario,
) -> None:
    # Given: selected active, archived, already-read rows plus an unselected unread row.
    scenario = archive_scenario
    active = await scenario.notice()
    archived = await scenario.notice()
    read = await scenario.notice(is_read=True)
    await scenario.notice()
    assert await notification_dao.archive_for_user(archived.id, scenario.owner.id)
    before = await scenario.db.fetchall("SELECT * FROM notifications ORDER BY id")
    selected = [active.id, archived.id, read.id]
    # When: the same duplicate-containing selection is submitted twice.
    responses = [
        await scenario.client.post(
            "/api/notifications/read-selected",
            headers=scenario.headers(scenario.owner),
            json={"ids": [str(note_id) for note_id in [*selected, archived.id]]},
        )
        for _attempt in range(2)
    ]
    # Then: only is_read changes; replay neither restores archived rows nor reads unselected rows.
    assert [response.status_code for response in responses] == [200, 200]
    assert all(response.json() == {"ok": True} for response in responses)
    assert await scenario.db.fetchall("SELECT * FROM notifications ORDER BY id") == [
        {**row, "is_read": True} if row["id"] in selected else row for row in before
    ]
