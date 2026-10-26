from uuid import uuid4

import pytest
from psycopg.errors import CheckViolation

from app.dao.agent_dao import agent_dao
from app.dao.notification_dao import notification_dao
from notification_archive_fixtures import ArchiveScenario


@pytest.mark.parametrize("invalid_recipient", ["colleague", "outsider", "missing"])
async def test_selected_read_rejects_mixed_invalid_ids_atomically(
    archive_scenario: ArchiveScenario, invalid_recipient: str
) -> None:
    # Given: two valid unread rows and an invalid selector.
    scenario = archive_scenario
    first = await scenario.notice()
    second = await scenario.notice()
    foreign = await notification_dao.create(
        obj_in={"user_id": scenario.colleague.id, "type": "broadcast", "title": "Colleague"}
    )
    outsider = await notification_dao.create(
        obj_in={"user_id": scenario.outsider.id, "type": "broadcast", "title": "Outsider"}
    )
    selectors = {"colleague": foreign.id, "outsider": outsider.id, "missing": uuid4()}
    before = await scenario.db.fetchall("SELECT * FROM notifications ORDER BY id")
    # When
    response = await scenario.client.post(
        "/api/notifications/read-selected",
        headers=scenario.headers(scenario.owner),
        json={"ids": [str(first.id), str(selectors[invalid_recipient]), str(second.id), str(first.id)]},
    )
    # Then: a generic response leaks no offending IDs and no row changes.
    assert response.status_code == 404
    assert response.json() == {"detail": "Notification not found"}
    assert await scenario.db.fetchall("SELECT * FROM notifications ORDER BY id") == before


@pytest.mark.parametrize("reference", ["agent_id", "ref_id", "link"])
@pytest.mark.parametrize("archived", [False, True])
async def test_selected_read_rejects_hidden_rows_even_when_archived_or_read(
    archive_scenario: ArchiveScenario, reference: str, archived: bool
) -> None:
    # Given: an owned notice hidden by the shared retained-agent visibility predicate.
    scenario = archive_scenario
    visible = await scenario.notice()
    agent = await agent_dao.create(
        obj_in={"name": "Retained", "creator_id": scenario.owner.id, "tenant_id": scenario.owner.tenant_id}
    )
    transfer_id = uuid4()
    await scenario.db.execute(
        "INSERT INTO company_transfer_requests "
        "(id, applicant_user_id, source_tenant_id, target_tenant_id, choice, method, "
        "fee_amount_minor, currency, instructions) "
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
    references = {
        "agent_id": {"agent_id": agent.id},
        "ref_id": {"ref_id": agent.id},
        "link": {"link": f"/app/agents/{agent.id}"},
    }
    hidden = await notification_dao.create(
        obj_in={
            "user_id": scenario.owner.id,
            "type": "broadcast",
            "title": "Hidden",
            "is_read": archived,
            **references[reference],
        }
    )
    if archived:
        await scenario.db.execute("UPDATE notifications SET archived_at = now() WHERE id = %(id)s", {"id": hidden.id})
    before = await scenario.db.fetchall("SELECT * FROM notifications ORDER BY id")
    # When: valid and hidden selectors appear in one batch.
    response = await scenario.client.post(
        "/api/notifications/read-selected",
        headers=scenario.headers(scenario.owner),
        json={"ids": [str(visible.id), str(hidden.id)]},
    )
    # Then: even an already-read hidden selector invalidates the entire set.
    assert response.status_code == 404
    assert response.json() == {"detail": "Notification not found"}
    assert await scenario.db.fetchall("SELECT * FROM notifications ORDER BY id") == before


async def test_selected_read_rolls_back_when_commit_fails(archive_scenario: ArchiveScenario) -> None:
    # Given: a deferred database failure after the batch update executes.
    scenario = archive_scenario
    notices = [await scenario.notice(), await scenario.notice()]
    await scenario.db.execute(
        "CREATE FUNCTION reject_selected_read() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'selected read rejected' USING ERRCODE = '23514'; END; $$"
    )
    await scenario.db.execute(
        "CREATE CONSTRAINT TRIGGER reject_selected_read AFTER UPDATE ON notifications "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_selected_read()"
    )
    before = await scenario.db.fetchall("SELECT * FROM notifications ORDER BY id")
    # When
    with pytest.raises(CheckViolation, match="selected read rejected"):
        await scenario.client.post(
            "/api/notifications/read-selected",
            headers=scenario.headers(scenario.owner),
            json={"ids": [str(notice.id) for notice in notices]},
        )
    # Then: both updates were rolled back, including on a reused request connection.
    assert await scenario.db.fetchall("SELECT * FROM notifications ORDER BY id") == before


@pytest.mark.parametrize("count", [200, 201])
@pytest.mark.parametrize("duplicates", [False, True])
async def test_selected_read_enforces_submitted_upper_bound_in_postgres(
    archive_scenario: ArchiveScenario, count: int, duplicates: bool
) -> None:
    # Given: a submitted set at or beyond the limit, optionally all the same UUID.
    scenario = archive_scenario
    ids = [uuid4() for _index in range(count)]
    await scenario.db.executemany(
        "INSERT INTO notifications (id, user_id, type, title, body, is_read) "
        "VALUES (%(id)s, %(user_id)s, 'broadcast', 'Notice', '', FALSE)",
        [{"id": note_id, "user_id": scenario.owner.id} for note_id in ids],
    )
    selected = [ids[0]] * count if duplicates else ids
    # When
    response = await scenario.client.post(
        "/api/notifications/read-selected",
        headers=scenario.headers(scenario.owner),
        json={"ids": [str(note_id) for note_id in selected]},
    )
    # Then: the original submitted count, not the distinct count, determines acceptance.
    assert response.status_code == (200 if count == 200 else 422)
    stored = await scenario.db.fetchall("SELECT id, is_read FROM notifications")
    assert {row["id"] for row in stored if row["is_read"]} == (set(selected) if count == 200 else set())
