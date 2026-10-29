from uuid import UUID, uuid4

import pytest

from app.records.agent import AgentRecord
from app.services import openclaw_inbox
from app.services.agent_manager import agent_manager
from app.services.guest_identity import guest_container_name
from guest_identity_fakes import Docker, Guest

FIRST = UUID("deadbeef-0000-4000-8000-000000000001")
SECOND = UUID("deadbeef-0000-4000-8000-000000000002")


@pytest.mark.parametrize("recorded", [None, "legacy-id", "stale-id"])
def test_exec_routes_to_label_proven_legacy_id(monkeypatch: pytest.MonkeyPatch, recorded: str | None) -> None:
    # Given a deployed short-name guest with a complete matching label.
    docker = Docker(Guest("legacy-id", "maraclaw-agent-deadbeef", str(FIRST)))
    monkeypatch.setattr(agent_manager, "docker_client", docker)
    agent = AgentRecord(id=FIRST, name="Guest", creator_id=uuid4(), container_id=recorded)
    # When delivering an inbox command.
    result = openclaw_inbox._docker_execute(agent, ["node", "wake.js"])
    # Then execution uses its inspected immutable ID, not the colliding name or stale persisted ID.
    assert result is None
    assert docker.container.calls == [("execute", "legacy-id")]


def test_exec_never_routes_through_foreign_legacy_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given the short-name collision belongs to another complete UUID.
    docker = Docker(Guest("foreign-id", "maraclaw-agent-deadbeef", str(SECOND)))
    monkeypatch.setattr(agent_manager, "docker_client", docker)
    agent = AgentRecord(id=FIRST, name="Guest", creator_id=uuid4())
    # When delivering an inbox command without a recorded container.
    result = openclaw_inbox._docker_execute(agent, ["node", "wake.js"])
    # Then no command is delivered to the foreign guest.
    assert result is not None
    assert docker.container.calls == []


def test_inbox_and_manager_share_full_uuid_naming() -> None:
    # Given UUIDs that share their first eight characters.
    # When deriving inbox destinations.
    names = {guest_container_name(FIRST), guest_container_name(SECOND)}
    # Then both destinations are independently routable by complete UUID.
    assert names == {f"maraclaw-agent-{FIRST}", f"maraclaw-agent-{SECOND}"}
