import asyncio

import pytest

from app.dao.notification_dao import notification_dao
from app.db.pool import get_pool
from notification_archive_fixtures import ArchiveScenario


@pytest.mark.parametrize("operation", ["archive", "restore"])
async def test_selected_archive_replay_preserves_read_state(archive_scenario: ArchiveScenario, operation: str) -> None:
    # Given two selected rows with different read states and one unselected row.
    scenario = archive_scenario
    notices = [await scenario.notice(), await scenario.notice(is_read=True)]
    await scenario.notice()
    if operation == "restore":
        for notice in notices:
            assert await notification_dao.archive_for_user(notice.id, scenario.owner.id)
    before = await scenario.db.fetchall("SELECT * FROM notifications ORDER BY id")
    payload = {"ids": [str(notices[1].id), str(notices[0].id), str(notices[0].id)]}
    # When the same selection is submitted twice.
    first = await scenario.client.post(
        f"/api/notifications/{operation}", headers=scenario.headers(scenario.owner), json=payload
    )
    after = await scenario.db.fetchall("SELECT * FROM notifications ORDER BY id")
    second = await scenario.client.post(
        f"/api/notifications/{operation}", headers=scenario.headers(scenario.owner), json=payload
    )
    # Then replay changes nothing and only selected archive timestamps differ from the original rows.
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"ok": True}
    assert await scenario.db.fetchall("SELECT * FROM notifications ORDER BY id") == after
    selected = {notice.id for notice in notices}
    for original, result in zip(before, after, strict=True):
        assert {**result, "archived_at": original["archived_at"]} == original
        if result["id"] in selected:
            assert (result["archived_at"] is not None) is (operation == "archive")
        else:
            assert result == original


@pytest.mark.parametrize("operation", ["archive", "restore"])
@pytest.mark.parametrize("ids", [[], ["not-a-uuid"], None])
async def test_selected_archive_rejects_invalid_input(
    archive_scenario: ArchiveScenario, operation: str, ids: object
) -> None:
    # Given malformed or empty input, when submitted, then validation rejects it.
    response = await archive_scenario.client.post(
        f"/api/notifications/{operation}", headers=archive_scenario.headers(archive_scenario.owner), json={"ids": ids}
    )
    assert response.status_code == 422


async def test_overlapping_selected_batches_are_consistent(archive_scenario: ArchiveScenario) -> None:
    # Given separate pool connections and overlapping selections in opposite order.
    scenario = archive_scenario
    notices = [await scenario.notice(), await scenario.notice(is_read=True)]
    await get_pool().resize(min_size=2, max_size=2)
    ids = [str(notice.id) for notice in notices]
    # When concurrent archive requests select the same rows in opposite order.
    async with asyncio.timeout(10):
        responses = await asyncio.gather(
            *(
                scenario.client.post(
                    "/api/notifications/archive", headers=scenario.headers(scenario.owner), json={"ids": batch}
                )
                for batch in (ids, list(reversed(ids)))
            )
        )
    # Then both complete without deadlock and both rows retain their read states.
    assert [response.status_code for response in responses] == [200, 200]
    rows = await scenario.db.fetchall("SELECT id, is_read, archived_at FROM notifications")
    assert {row["id"]: row["is_read"] for row in rows} == {notices[0].id: False, notices[1].id: True}
    assert all(row["archived_at"] is not None for row in rows)
