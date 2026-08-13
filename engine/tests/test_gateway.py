"""Regression coverage for gateway Feishu channel resolution."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import gateway
from app.records.agent import AgentRecord
from app.records.org import OrgMemberRecord
from app.schemas.schemas import GatewaySendMessageRequest


@pytest.mark.asyncio
async def test_send_message_rejects_cross_tenant_feishu_fallback_when_no_tenant_config_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid.uuid4()
    source_agent = AgentRecord(
        id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        name="Sending agent",
        tenant_id=tenant_id,
    )
    member = OrgMemberRecord(
        id=uuid.uuid4(),
        name="Recipient",
        external_id="ou_recipient",
        status="active",
        tenant_id=tenant_id,
    )
    relationship = SimpleNamespace(
        agent_id=source_agent.id,
        member_id=member.id,
        member=member,
        relation="collaborator",
        description="",
    )

    agent_lookup_calls: list[uuid.UUID] = []
    tenant_lookup_calls: list[tuple[uuid.UUID, str]] = []

    async def get_source_agent(_api_key: str, _db=None) -> AgentRecord:
        return source_agent

    async def active_human_relationship(
        _db,
        _relationship,
        *,
        source_agent: AgentRecord | None = None,
    ) -> dict[str, str]:
        _ = source_agent
        return {"access_status": "active"}

    async def no_agent_rels(_agent_id: uuid.UUID):
        return []

    async def human_rels(_agent_id: uuid.UUID):
        return [relationship]

    async def no_agent_feishu(*, agent_id: uuid.UUID, channel_type: str):
        agent_lookup_calls.append(agent_id)
        assert channel_type == "feishu"
        return

    async def no_tenant_feishu(*, tenant_id: uuid.UUID, channel_type: str):
        tenant_lookup_calls.append((tenant_id, channel_type))
        return

    async def touch_last_seen(*, db_obj, obj_in):
        return db_obj

    monkeypatch.setattr(gateway, "_get_agent_by_key", get_source_agent)
    monkeypatch.setattr(gateway, "evaluate_human_relationship_status", active_human_relationship)
    monkeypatch.setattr(gateway.agent_agent_relationship_dao, "list_for_agent_with_targets", no_agent_rels)
    monkeypatch.setattr(gateway.agent_relationship_dao, "list_for_agent_with_members", human_rels)
    monkeypatch.setattr(gateway.channel_config_dao, "get_for_agent", no_agent_feishu)
    monkeypatch.setattr(gateway.channel_config_dao, "get_for_tenant_channel", no_tenant_feishu)
    monkeypatch.setattr(gateway.agent_dao, "update", touch_last_seen)

    with pytest.raises(HTTPException) as error:
        await gateway.send_message(
            GatewaySendMessageRequest(target=member.name, content="Hello"),
            "api-key",
            None,
        )

    assert error.value.status_code == 400
    assert error.value.detail == "No Feishu channel configured"
    assert agent_lookup_calls == [source_agent.id]
    assert tenant_lookup_calls == [(tenant_id, "feishu")]
