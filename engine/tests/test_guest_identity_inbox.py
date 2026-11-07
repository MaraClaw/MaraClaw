from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services import openclaw_gateway_config, openclaw_gateway_rpc
from app.services.agent_manager import agent_manager
from app.services.guest_identity import guest_container_name
from guest_identity_fakes import Docker, Guest

FIRST = UUID("deadbeef-0000-4000-8000-000000000001")
SECOND = UUID("deadbeef-0000-4000-8000-000000000002")


@pytest.mark.parametrize("recorded", [None, "legacy-id", "stale-id"])
async def test_exec_routes_to_label_proven_legacy_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path, recorded: str | None
) -> None:
    # Given a deployed short-name guest with a complete matching label.
    docker = Docker(Guest("legacy-id", "maraclaw-agent-deadbeef", str(FIRST)))
    inspect = docker.container.inspect

    def inspect_running(reference):
        container = inspect(reference)
        container.state = SimpleNamespace(running=True)
        return container

    monkeypatch.setattr(docker.container, "inspect", inspect_running)
    monkeypatch.setattr(agent_manager, "docker_client", docker)
    agent = AgentRecord(id=FIRST, name="Guest", creator_id=uuid4(), container_id=recorded)
    message = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="hello")
    targets: list[str] = []
    _prepare_execution(monkeypatch, tmp_path, agent, targets)
    result = await openclaw_gateway_rpc.execute_gateway_turn(agent, message)
    assert targets == ["legacy-id"]
    assert result == openclaw_gateway_rpc.UncertainOutcome(str(message.id), "unknown", None, "invalid_output")


async def test_exec_never_routes_through_foreign_legacy_name(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # Given the short-name collision belongs to another complete UUID.
    docker = Docker(Guest("foreign-id", "maraclaw-agent-deadbeef", str(SECOND)))
    monkeypatch.setattr(agent_manager, "docker_client", docker)
    agent = AgentRecord(id=FIRST, name="Guest", creator_id=uuid4())
    message = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="hello")
    targets: list[str] = []
    _prepare_execution(monkeypatch, tmp_path, agent, targets)
    result = await openclaw_gateway_rpc.execute_gateway_turn(agent, message)
    # Then no command is delivered to the foreign guest.
    assert result == openclaw_gateway_rpc.NotAdmittedOutcome(str(message.id), "not_submitted", "guest_unavailable")
    assert targets == []


def _prepare_execution(monkeypatch, tmp_path, agent: AgentRecord, targets: list[str]) -> None:
    @asynccontextmanager
    async def exclusion(_agent_id):
        yield

    async def allowed(_agent_id):
        return None

    async def fresh(_agent_id):
        return agent

    def execute(container_id, _payload_name, _cancel):
        targets.append(container_id)
        return "{}"

    monkeypatch.setattr(openclaw_gateway_rpc, "guest_write_section", exclusion)
    monkeypatch.setattr(openclaw_gateway_rpc, "assert_work_allowed", allowed)
    monkeypatch.setattr(openclaw_gateway_rpc.agent_dao, "get", fresh)
    monkeypatch.setattr(openclaw_gateway_rpc, "_execute", execute)
    monkeypatch.setattr(agent_manager, "_agent_dir", lambda _agent_id: tmp_path)
    monkeypatch.setattr(openclaw_gateway_config, "prepare_native_gateway_files", lambda _directory: None)


def test_inbox_and_manager_share_full_uuid_naming() -> None:
    # Given UUIDs that share their first eight characters.
    # When deriving inbox destinations.
    names = {guest_container_name(FIRST), guest_container_name(SECOND)}
    # Then both destinations are independently routable by complete UUID.
    assert names == {f"maraclaw-agent-{FIRST}", f"maraclaw-agent-{SECOND}"}
