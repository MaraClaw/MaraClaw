import base64
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException

from app.api import relationships
from app.records.agent import AgentRecord
from app.records.agent_relationship import AgentRelationshipRecord
from app.records.user import UserRecord
from app.schemas.collaboration_response_relationships import HumanRelationshipCursor


def test_new_relationship_operations_publish_generated_response_contracts() -> None:
    app = FastAPI()
    app.include_router(relationships.router, prefix="/api")

    document = app.openapi()
    paths = document["paths"]
    for path, method, model in (
        ("/api/agents/{agent_id}/relationships/page", "get", "HumanRelationshipPageResponse"),
        ("/api/agents/{agent_id}/relationships/", "post", "CollaborationAcknowledgement"),
    ):
        schema = paths[path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{model}"}


@pytest.fixture
def page_context(monkeypatch: pytest.MonkeyPatch) -> tuple[AgentRecord, UserRecord]:
    actor = UserRecord(uuid4(), tenant_id=uuid4())
    agent = AgentRecord(uuid4(), "Paged", actor.id, tenant_id=actor.tenant_id)
    monkeypatch.setattr(relationships, "check_agent_access", AsyncMock(return_value=(agent, "manage")))
    monkeypatch.setattr(relationships, "ensure_access_granted_platform_relationships", AsyncMock(return_value=False))
    monkeypatch.setattr(relationships.agent_relationship_dao, "list_page_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        relationships.agent_relationship_dao, "list_for_agent_with_members_and_providers", AsyncMock(return_value=[])
    )
    return agent, actor


@pytest.mark.parametrize("size", [0, 2, 3])
async def test_relationship_page_hydrates_only_retained_rows(
    page_context: tuple[AgentRecord, UserRecord], monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    agent, actor = page_context
    ids = [UUID(int=index + 1) for index in range(size)]
    rows = [AgentRelationshipRecord(key, agent.id, uuid4()) for key in ids[:2]]
    keys = AsyncMock(return_value=ids)
    hydrate = AsyncMock(return_value=rows)
    status = AsyncMock(
        return_value={"access_allowed": False, "access_status": "missing_target", "access_status_reason": None}
    )
    monkeypatch.setattr(relationships.agent_relationship_dao, "list_page_ids", keys)
    monkeypatch.setattr(relationships.agent_relationship_dao, "list_for_agent_with_members_and_providers", hydrate)
    monkeypatch.setattr(relationships, "evaluate_human_relationship_status", status)

    page = await relationships.get_relationship_page(agent.id, actor, limit=2)

    assert [row["id"] for row in page["items"]] == [str(key) for key in ids[:2]]
    assert page["has_more"] is (size > 2)
    assert (page["next_cursor"] is not None) is (size > 2)
    hydrate.assert_awaited_once_with(agent.id, relationship_ids=ids[:2])
    assert status.await_count == min(size, 2)
    if page["next_cursor"] is not None:
        encoded = page["next_cursor"]
        cursor = HumanRelationshipCursor.model_validate_json(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        assert (cursor.agent_id, cursor.tenant_id, cursor.relationship_id) == (agent.id, agent.tenant_id, ids[1])


@pytest.mark.parametrize("kind", ["malformed", "empty", "version", "missing_version", "agent", "tenant"])
async def test_relationship_page_rejects_invalid_or_foreign_cursors_before_reconciliation(
    page_context: tuple[AgentRecord, UserRecord], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    agent, actor = page_context
    cursor = HumanRelationshipCursor(
        version=1,
        agent_id=uuid4() if kind == "agent" else agent.id,
        tenant_id=uuid4() if kind == "tenant" else agent.tenant_id,
        relationship_id=uuid4(),
    ).model_dump_json()
    if kind == "version":
        cursor = cursor.replace('"version":1', '"version":2')
    if kind == "missing_version":
        cursor = cursor.replace('"version":1,', "")
    raw = base64.urlsafe_b64encode(cursor.encode()).decode().rstrip("=")
    if kind == "malformed":
        raw = "!"
    if kind == "empty":
        raw = ""
    reconcile = AsyncMock()
    monkeypatch.setattr(relationships, "ensure_access_granted_platform_relationships", reconcile)

    with pytest.raises(HTTPException) as error:
        await relationships.get_relationship_page(agent.id, actor, cursor=raw)

    assert error.value.status_code == 400
    reconcile.assert_not_awaited()
