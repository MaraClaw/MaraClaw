import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.core import permissions
from app.dao.agent_agent_relationship_dao import agent_agent_relationship_dao
from app.dao.agent_relationship_dao import agent_relationship_dao
from app.records.agent import AgentRecord
from app.records.agent_agent_relationship import AgentAgentRelationshipRecord
from app.records.agent_relationship import AgentRelationshipRecord
from app.records.org import OrgMemberRecord
from app.services.agent_context import _load_relationships_from_db


@pytest.mark.asyncio
async def test_build_agent_context_does_not_inject_legacy_focus_file():
    from app.services.agent_context import build_agent_context

    agent_id = uuid.uuid4()

    async def fake_read_file(key, _max_chars=3000):
        if key == f"{agent_id}/focus.md":
            return "# Focus\n\n- [ ] follow_up: Check the deployment"
        return ""

    with (
        patch("app.services.agent_context._read_file_safe", side_effect=fake_read_file),
        patch("app.services.agent_context._load_skills_index", new_callable=AsyncMock, return_value=""),
        patch("app.services.agent_context._load_relationships_from_db", new_callable=AsyncMock, return_value=""),
        patch("app.services.timezone_utils.get_agent_timezone", new_callable=AsyncMock, return_value="UTC"),
    ):
        _static, dynamic = await build_agent_context(agent_id, "TestAgent")

    assert "## Focus" not in dynamic
    assert "follow_up: Check the deployment" not in dynamic


def _human(agent_id: uuid.UUID, name: str, provider: str | None = None) -> AgentRelationshipRecord:
    member = OrgMemberRecord(uuid.uuid4(), name, title="Lead", status="active")
    return AgentRelationshipRecord(
        uuid.uuid4(), agent_id, member.id, description=f"{name} note", member=member, provider_name=provider
    )


def _peer(agent_id: uuid.UUID, name: str) -> AgentAgentRelationshipRecord:
    target = AgentRecord(uuid.uuid4(), name, uuid.uuid4(), role_description="Analyst")
    return AgentAgentRelationshipRecord(
        uuid.uuid4(), agent_id, target.id, description=f"{name} note", target_agent=target
    )


def _statuses(batch: list[Any], restricted: set[uuid.UUID]) -> list[dict[str, Any]]:
    return [
        {
            "access_allowed": rel.id not in restricted,
            "access_status": "restricted" if rel.id in restricted else "active",
            "access_status_reason": None,
        }
        for rel in batch
    ]


async def test_load_relationships_evaluates_each_kind_in_one_batch_and_lists_only_active_rows(
    monkeypatch: pytest.MonkeyPatch,
):
    agent_id = uuid.uuid4()
    humans = [_human(agent_id, "Dana", "Feishu"), _human(agent_id, "Eli"), _human(agent_id, "Fay")]
    peers = [_peer(agent_id, "Alpha"), _peer(agent_id, "Beta")]
    restricted = {humans[1].id, peers[1].id}
    human_batches: list[list[Any]] = []
    agent_batches: list[list[Any]] = []

    async def evaluate_humans(batch: list[Any]) -> list[dict[str, Any]]:
        human_batches.append(list(batch))
        return _statuses(batch, restricted)

    async def evaluate_agents(batch: list[Any]) -> list[dict[str, Any]]:
        agent_batches.append(list(batch))
        return _statuses(batch, restricted)

    monkeypatch.setattr(
        agent_relationship_dao, "list_for_agent_with_members_and_providers", AsyncMock(return_value=humans)
    )
    monkeypatch.setattr(agent_agent_relationship_dao, "list_for_agent_with_targets", AsyncMock(return_value=peers))
    monkeypatch.setattr(permissions, "evaluate_human_relationship_statuses", evaluate_humans)
    monkeypatch.setattr(permissions, "evaluate_agent_relationship_statuses", evaluate_agents)

    text = await _load_relationships_from_db(agent_id)

    assert human_batches == [humans]
    assert agent_batches == [peers]
    assert "### Dana - Lead (synced through Feishu)" in text
    assert "- Dana note" in text
    assert text.index("### Dana") < text.index("### Fay - Lead")
    assert "### Alpha - Analyst" in text
    assert "Eli" not in text
    assert "Beta" not in text


async def test_load_relationships_is_empty_when_no_row_is_active(monkeypatch: pytest.MonkeyPatch):
    agent_id = uuid.uuid4()
    human, peer = _human(agent_id, "Dana"), _peer(agent_id, "Alpha")
    monkeypatch.setattr(
        agent_relationship_dao, "list_for_agent_with_members_and_providers", AsyncMock(return_value=[human])
    )
    monkeypatch.setattr(agent_agent_relationship_dao, "list_for_agent_with_targets", AsyncMock(return_value=[peer]))
    monkeypatch.setattr(
        permissions,
        "evaluate_human_relationship_statuses",
        AsyncMock(side_effect=lambda batch: _statuses(batch, {human.id})),
    )
    monkeypatch.setattr(
        permissions,
        "evaluate_agent_relationship_statuses",
        AsyncMock(side_effect=lambda batch: _statuses(batch, {peer.id})),
    )

    assert await _load_relationships_from_db(agent_id) == ""
