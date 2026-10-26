from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter

from app.dao.agent_dao import agent_dao
from app.schemas.notification_responses import NotificationPageResponse
from notification_archive_fixtures import ArchiveScenario, archive_scenario, schema_db

__all__ = ["archive_scenario", "schema_db"]
PAGE_RESPONSE = TypeAdapter(NotificationPageResponse)


@pytest.mark.parametrize("view", ["active", "archived"])
@pytest.mark.parametrize("reference", ["agent_id", "ref_id", "link"])
async def test_retained_agent_visibility_applies_to_page_boundaries_and_lookahead(
    archive_scenario: ArchiveScenario, view: Literal["active", "archived"], reference: str
) -> None:
    # Given: hidden retained-agent rows appear both before and between visible rows at a tied timestamp.
    scenario = archive_scenario
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
    stamp = datetime(2026, 10, 23, tzinfo=UTC)
    await scenario.db.executemany(
        "INSERT INTO notifications (id, user_id, type, title, body, is_read, agent_id, ref_id, link, created_at, archived_at) "
        "VALUES (%(id)s, %(user)s, 'broadcast', 'Notice', '', FALSE, %(agent)s, %(ref)s, %(link)s, %(stamp)s, %(archive)s)",
        [
            {
                "id": UUID(int=index),
                "user": scenario.owner.id,
                "stamp": stamp,
                "archive": stamp if view == "archived" else None,
                "agent": agent.id if index % 2 == 0 and reference == "agent_id" else None,
                "ref": agent.id if index % 2 == 0 and reference == "ref_id" else None,
                "link": f"/app/agents/{agent.id}" if index % 2 == 0 and reference == "link" else None,
            }
            for index in range(1, 5)
        ],
    )
    headers = scenario.headers(scenario.owner)
    params = {"view": view, "category": "social", "limit": "1"}
    # When: both pages are fetched through the authenticated HTTP router and real DAO.
    first = await scenario.client.get("/api/notifications/page", params=params, headers=headers)
    assert first.status_code == 200
    first_page = PAGE_RESPONSE.validate_json(first.content)
    cursor = first_page["next_cursor"]
    assert cursor is not None
    second = await scenario.client.get("/api/notifications/page", params={**params, "cursor": cursor}, headers=headers)
    # Then: hidden rows never appear, become boundaries, or cause a spurious continuation.
    assert second.status_code == 200
    second_page = PAGE_RESPONSE.validate_json(second.content)
    assert [row["id"] for row in first_page["items"] + second_page["items"]] == [str(UUID(int=3)), str(UUID(int=1))]
    assert second_page["next_cursor"] is None
