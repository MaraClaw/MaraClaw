from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from app.records.agent import AgentRecord
from app.services import openclaw_inbox
from app.services.agent_manager import agent_manager
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


@pytest.mark.parametrize("legacy_owner", [str(FIRST), str(SECOND)])
async def test_http_routes_only_to_label_proven_guest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, legacy_owner: str
) -> None:
    # Given a legacy name and a stale published port that could belong to a different guest.
    docker = Docker(Guest("legacy-id", "maraclaw-agent-deadbeef", legacy_owner))
    monkeypatch.setattr(agent_manager, "docker_client", docker)
    agent = AgentRecord(id=FIRST, name="Guest", creator_id=uuid4(), container_port=19876)
    urls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(401)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(openclaw_inbox.httpx, "AsyncClient", lambda **_kwargs: client)
    # When hooks delivery is attempted.
    error = await openclaw_inbox._http_hooks_wake(agent, tmp_path, openclaw_inbox._wake_body("hello"))
    # Then only a proven name is tried, never an unverified host-port fallback.
    assert error is not None
    assert urls == (["http://maraclaw-agent-deadbeef:18789/hooks/agent"] if legacy_owner == str(FIRST) else [])


def test_inbox_and_manager_share_full_uuid_naming() -> None:
    # Given UUIDs that share their first eight characters.
    # When deriving inbox destinations.
    names = {openclaw_inbox.openclaw_container_name(FIRST), openclaw_inbox.openclaw_container_name(SECOND)}
    # Then both destinations are independently routable by complete UUID.
    assert names == {f"maraclaw-agent-{FIRST}", f"maraclaw-agent-{SECOND}"}


async def test_http_retains_the_inspect_proven_published_port_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given an owned legacy guest whose inspected port agrees with its persisted port.
    docker = Docker(Guest("legacy-id", "maraclaw-agent-deadbeef", str(FIRST), published_port="19876"))
    monkeypatch.setattr(agent_manager, "docker_client", docker)
    agent = AgentRecord(id=FIRST, name="Guest", creator_id=uuid4(), container_id="legacy-id", container_port=19876)
    urls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.host == "maraclaw-agent-deadbeef":
            raise httpx.ConnectError("DNS unavailable", request=request)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(openclaw_inbox.httpx, "AsyncClient", lambda **_kwargs: client)
    # When Docker DNS is unreachable but the proven published endpoint accepts the wake.
    error = await openclaw_inbox._http_hooks_wake(agent, tmp_path, openclaw_inbox._wake_body("hello"))
    # Then the existing host fallback still delivers to this guest.
    assert error is None
    assert urls == ["http://maraclaw-agent-deadbeef:18789/hooks/agent", "http://host.docker.internal:19876/hooks/agent"]
