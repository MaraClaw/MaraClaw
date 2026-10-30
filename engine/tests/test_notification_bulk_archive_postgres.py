from datetime import datetime
from uuid import uuid4

import anyio
import pytest
from psycopg.errors import CheckViolation

from app.dao.agent_dao import agent_dao
from app.dao.notification_dao import notification_dao
from notification_archive_fixtures import ArchiveScenario, archive_scenario, schema_db

__all__ = ["archive_scenario", "schema_db"]


@pytest.mark.parametrize("operation", ["archive", "restore"])
async def test_selected_mutation_preserves_read_state_and_is_idempotent(
    archive_scenario: ArchiveScenario, operation: str
) -> None:
    scenario = archive_scenario
    notices = [await scenario.notice(), await scenario.notice(is_read=True)]
    if operation == "restore":
        for notice in notices:
            assert await notification_dao.archive_for_user(notice.id, scenario.owner.id)
    payload = {"ids": [str(notices[1].id), str(notices[0].id), str(notices[0].id)]}
    headers = scenario.headers(scenario.owner)
    first = await scenario.client.post(f"/api/notifications/{operation}", headers=headers, json=payload)
    before = await scenario.db.fetchall("SELECT id, is_read, archived_at FROM notifications ORDER BY id")
    replay = await scenario.client.post(f"/api/notifications/{operation}", headers=headers, json=payload)
    after = await scenario.db.fetchall("SELECT id, is_read, archived_at FROM notifications ORDER BY id")
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json() == {"ok": True}
    assert before == after
    assert {row["id"]: row["is_read"] for row in after} == {notices[0].id: False, notices[1].id: True}
    assert all(isinstance(row["archived_at"], datetime) == (operation == "archive") for row in after)


@pytest.mark.parametrize("operation", ["archive", "restore"])
@pytest.mark.parametrize("invalid", ["foreign", "missing", "hidden"])
async def test_selected_mutation_rejects_inaccessible_batches_atomically(
    archive_scenario: ArchiveScenario, operation: str, invalid: str
) -> None:
    scenario = archive_scenario
    visible = await scenario.notice()
    foreign = await notification_dao.create(
        obj_in={"user_id": scenario.colleague.id, "type": "broadcast", "title": "Foreign"}
    )
    agent = await agent_dao.create(
        obj_in={"name": "Retained", "creator_id": scenario.owner.id, "tenant_id": scenario.owner.tenant_id}
    )
    transfer_id = uuid4()
    await scenario.db.execute(
        "INSERT INTO company_transfer_requests "
        "(id, applicant_user_id, source_tenant_id, target_tenant_id, choice, method, fee_amount_minor, currency, instructions) "
        "VALUES (%(id)s, %(user)s, %(source)s, %(target)s, 'paid', 'sso', 0, 'USD', 'Test')",
        {
            "id": transfer_id,
            "user": scenario.owner.id,
            "source": scenario.owner.tenant_id,
            "target": scenario.outsider.tenant_id,
        },
    )
    await scenario.db.execute(
        "INSERT INTO company_transfer_retained_agents "
        "(agent_id, transfer_request_id, original_user_id, source_tenant_id, holder_user_id) "
        "VALUES (%(agent)s, %(transfer)s, %(user)s, %(tenant)s, %(holder)s)",
        {
            "agent": agent.id,
            "transfer": transfer_id,
            "user": scenario.owner.id,
            "tenant": scenario.owner.tenant_id,
            "holder": scenario.colleague.id,
        },
    )
    hidden = await notification_dao.create(
        obj_in={"user_id": scenario.owner.id, "type": "broadcast", "title": "Hidden", "agent_id": agent.id}
    )
    if operation == "restore":
        assert await notification_dao.archive_for_user(visible.id, scenario.owner.id)
    before = await scenario.db.fetchall("SELECT * FROM notifications ORDER BY id")
    invalid_id = {"foreign": foreign.id, "missing": uuid4(), "hidden": hidden.id}[invalid]
    response = await scenario.client.post(
        f"/api/notifications/{operation}",
        headers=scenario.headers(scenario.owner),
        json={"ids": [str(visible.id), str(invalid_id)]},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Notification not found"}
    assert await scenario.db.fetchall("SELECT * FROM notifications ORDER BY id") == before


@pytest.mark.parametrize("operation", ["archive", "restore"])
@pytest.mark.parametrize("count", [0, 200, 201])
async def test_selected_mutation_validates_submitted_count_before_deduplication(
    archive_scenario: ArchiveScenario, operation: str, count: int
) -> None:
    scenario = archive_scenario
    notice = await scenario.notice()
    response = await scenario.client.post(
        f"/api/notifications/{operation}",
        headers=scenario.headers(scenario.owner),
        json={"ids": [str(notice.id)] * count},
    )
    assert response.status_code == (200 if count == 200 else 422)


@pytest.mark.parametrize("operation", ["archive", "restore"])
async def test_selected_mutation_rolls_back_on_commit_failure(
    archive_scenario: ArchiveScenario, operation: str
) -> None:
    scenario = archive_scenario
    notices = [await scenario.notice(), await scenario.notice()]
    if operation == "restore":
        for notice in notices:
            assert await notification_dao.archive_for_user(notice.id, scenario.owner.id)
    await scenario.db.execute(
        "CREATE FUNCTION reject_bulk_archive() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'rejected' USING ERRCODE = '23514'; END; $$"
    )
    await scenario.db.execute(
        "CREATE CONSTRAINT TRIGGER reject_bulk_archive AFTER UPDATE ON notifications "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_bulk_archive()"
    )
    before = await scenario.db.fetchall("SELECT * FROM notifications ORDER BY id")
    with pytest.raises(CheckViolation, match="rejected"):
        await scenario.client.post(
            f"/api/notifications/{operation}",
            headers=scenario.headers(scenario.owner),
            json={"ids": [str(notice.id) for notice in notices]},
        )
    assert await scenario.db.fetchall("SELECT * FROM notifications ORDER BY id") == before


async def test_overlapping_selected_archives_complete_without_deadlock(archive_scenario: ArchiveScenario) -> None:
    scenario = archive_scenario
    notices = [await scenario.notice(), await scenario.notice(is_read=True)]
    statuses: list[int] = []

    async def submit(ids: list[str]) -> None:
        response = await scenario.client.post(
            "/api/notifications/archive", headers=scenario.headers(scenario.owner), json={"ids": ids}
        )
        statuses.append(response.status_code)

    ids = [str(notice.id) for notice in notices]
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(submit, ids)
            group.start_soon(submit, list(reversed(ids)))
    assert statuses == [200, 200]
    rows = await scenario.db.fetchall("SELECT id, is_read, archived_at FROM notifications")
    assert {row["id"]: row["is_read"] for row in rows} == {notices[0].id: False, notices[1].id: True}
    assert all(row["archived_at"] is not None for row in rows)
