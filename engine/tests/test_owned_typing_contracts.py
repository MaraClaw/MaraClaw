from uuid import UUID, uuid4

import pytest

from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
from app.records.okr import OKRSettingsRecord
from app.services import okr_reporting, openclaw_inbox
from app.services.agent_manager import agent_manager


@pytest.mark.parametrize("nested", [False, True])
def test_docker_execute_accepts_direct_and_nested_clients(monkeypatch: pytest.MonkeyPatch, nested: bool) -> None:
    # Given: both supported SDK/fake shapes expose the same execute contract.
    calls: list[tuple[str, list[str], bool]] = []

    class Executor:
        def execute(self, container: str, command: list[str], *, stream: bool = False) -> str:
            calls.append((container, command, stream))
            return "done"

    class Client:
        container: Executor = Executor()

    monkeypatch.setattr(agent_manager, "docker_client", Client() if nested else Executor())
    agent = AgentRecord(id=uuid4(), name="Inbox", creator_id=uuid4(), container_id="container-abc")

    # When: the adapter invokes a guest command.
    error = openclaw_inbox._docker_execute(agent, ["node", "wake.mjs"])

    # Then: neither SDK shape changes command delivery or the success result.
    assert error is None
    assert calls == [("container-abc", ["node", "wake.mjs"], False)]


@pytest.mark.parametrize("primary_owner", ["same", "other", "global", "disabled"])
@pytest.mark.parametrize("agent_has_tenant", [True, False])
async def test_report_models_preserve_agent_tenant_ownership(
    monkeypatch: pytest.MonkeyPatch, primary_owner: str, agent_has_tenant: bool
) -> None:
    # Given: report settings select an agent with a valid fallback in its company.
    tenant_id = uuid4()
    primary = LLMModelRecord(
        id=uuid4(),
        provider="openai",
        model="primary",
        api_key_encrypted="",
        label="Primary",
        tenant_id=tenant_id if primary_owner in {"same", "disabled"} else uuid4() if primary_owner == "other" else None,
        enabled=primary_owner != "disabled",
    )
    fallback = LLMModelRecord(
        id=uuid4(),
        provider="openai",
        model="fallback",
        api_key_encrypted="",
        label="Fallback",
        tenant_id=tenant_id,
    )
    agent = AgentRecord(
        id=uuid4(),
        name="OKR",
        creator_id=uuid4(),
        tenant_id=tenant_id if agent_has_tenant else None,
        primary_model_id=primary.id,
        fallback_model_id=fallback.id,
    )
    settings = OKRSettingsRecord(tenant_id=tenant_id, okr_agent_id=agent.id)

    async def get_settings(requested_tenant: UUID) -> OKRSettingsRecord:
        assert requested_tenant == tenant_id
        return settings

    async def get_agent(agent_id: UUID) -> AgentRecord:
        assert agent_id == agent.id
        return agent

    async def get_model(model_id: UUID) -> LLMModelRecord | None:
        return {primary.id: primary, fallback.id: fallback}.get(model_id)

    monkeypatch.setattr(okr_reporting.okr_settings_dao, "get_by_tenant", get_settings)
    monkeypatch.setattr(okr_reporting.agent_dao, "get", get_agent)
    monkeypatch.setattr(okr_reporting.llm_model_dao, "get", get_model)

    # When: reporting resolves the agent's configured slots.
    result = await okr_reporting._resolve_report_models(tenant_id)

    # Then: invalid primary rows are rejected and only the owned fallback is promoted.
    expected_primary = (primary if primary_owner == "same" else fallback) if agent_has_tenant else None
    expected_fallback = fallback if agent_has_tenant and primary_owner == "same" else None
    assert result.primary is expected_primary
    assert result.fallback is expected_fallback
    assert result.okr_agent_id == agent.id
