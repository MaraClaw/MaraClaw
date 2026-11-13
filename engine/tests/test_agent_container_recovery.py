from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

import test_agent_lifecycle_http as lifecycle_http
from app.api import agents as api
from app.config import get_settings
from app.services import agent_container_recovery as recovery, agent_lifecycle as lifecycle
from app.services.agent_runtime_loss import recovery_snapshot_id
from app.services.openclaw_update_snapshot import snapshot_path

surface = lifecycle_http.surface
CONFIGURED = "openclaw-local:configured"
PINNED = "sha256:gone"


@pytest.fixture
def recovering(surface, monkeypatch, tmp_path):
    app, runtime, agent = surface
    runtime.committed = replace(runtime.committed, openclaw_image_id=PINNED)
    state = SimpleNamespace(guests=[], present={CONFIGURED}, pins=[], removed=[], runtime=runtime)
    agent_dir = tmp_path / str(agent.id)
    agent_dir.mkdir()
    (agent_dir / "state.txt").write_text("before")

    @asynccontextmanager
    async def committed():
        yield object()

    async def unpin(_db, _agent_id, image):
        state.pins.append(("unpin", image))
        if runtime.committed.openclaw_image_id != image:
            return False
        runtime.committed = replace(runtime.committed, openclaw_image_id=None)
        return True

    async def repin(_db, _agent_id, image):
        state.pins.append(("repin", image))
        runtime.committed = replace(runtime.committed, openclaw_image_id=image)
        return True

    def stop(_client, _agent, *, remove):
        state.removed.append(remove)

    monkeypatch.setattr(get_settings(), "MARACLAW_OPENCLAW_IMAGE", CONFIGURED)
    client = SimpleNamespace(image=SimpleNamespace(exists=lambda reference: reference in state.present))
    monkeypatch.setattr(recovery.agent_manager, "docker_client", client)
    monkeypatch.setattr(recovery, "owned_guest_containers", lambda _client, _agent: state.guests)
    monkeypatch.setattr(recovery, "stop_guest_containers", stop)
    monkeypatch.setattr(recovery, "committed_start_connection", committed)
    monkeypatch.setattr(recovery.agent_runtime_loss_dao, "unpin_image", unpin)
    monkeypatch.setattr(recovery.agent_runtime_loss_dao, "repin_image", repin)
    return app, agent, agent_dir, state


async def post(app, agent):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(f"/api/agents/{agent.id}/start-new-container")


def forbid_start(monkeypatch):
    async def start(_record):
        pytest.fail("A refused recovery attempted a start")

    monkeypatch.setattr(lifecycle.agent_manager, "start_container", start)


async def test_use_only_member_cannot_start_a_new_container(recovering, monkeypatch):
    # Given a member with use access.
    app, agent, _agent_dir, state = recovering

    async def authorize(_user, _id):
        return replace(state.runtime.committed), "use"

    monkeypatch.setattr(api, "check_agent_access", authorize)
    forbid_start(monkeypatch)
    # When they request a new container.
    response = await post(app, agent)
    # Then management authority is required.
    assert response.status_code == 403


async def test_present_guest_container_is_refused(recovering, monkeypatch):
    # Given the agent still owns a guest container.
    app, agent, _agent_dir, state = recovering
    state.guests = [SimpleNamespace(id="still-here")]
    forbid_start(monkeypatch)
    # When a new container is requested.
    response = await post(app, agent)
    # Then ordinary Start is the right action and nothing changed.
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "guest_container_present"
    assert state.pins == []


async def test_missing_configured_image_is_refused_without_changes(recovering, monkeypatch):
    # Given neither the pinned nor the configured image exists.
    app, agent, agent_dir, state = recovering
    state.present = set()
    forbid_start(monkeypatch)
    # When a new container is requested.
    response = await post(app, agent)
    # Then the request is refused before any snapshot or pin change.
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "guest_image_unavailable"
    assert state.pins == []
    assert not snapshot_path(agent_dir, recovery_snapshot_id(agent.id)).parent.exists()


async def test_vanished_pin_is_cleared_snapshotted_and_started(recovering, monkeypatch):
    # Given the pinned image vanished while the configured image is present.
    app, agent, agent_dir, state = recovering
    seen = []

    async def start(record):
        seen.append((record.openclaw_image_id, snapshot_path(agent_dir, recovery_snapshot_id(agent.id)).exists()))
        record.status, record.container_id, record.container_port = "running", "fresh", 18889
        return "fresh"

    monkeypatch.setattr(lifecycle.agent_manager, "start_container", start)
    # When a new container is requested.
    response = await post(app, agent)
    # Then the start ran unpinned with a snapshot taken, and the snapshot is dropped after publication.
    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert state.runtime.committed.container_id == "fresh"
    assert seen == [(None, True)]
    assert state.pins == [("unpin", PINNED)]
    assert not snapshot_path(agent_dir, recovery_snapshot_id(agent.id)).exists()


async def test_failed_start_restores_snapshot_and_pin(recovering, monkeypatch):
    # Given the configured image is present but the new guest fails to start after writing state.
    app, agent, agent_dir, state = recovering

    async def start(record):
        (agent_dir / "state.txt").write_text("migrated")
        record.status, record.container_id, record.container_port = "error", None, None

    monkeypatch.setattr(lifecycle.agent_manager, "start_container", start)
    # When a new container is requested.
    response = await post(app, agent)
    # Then the /start failure code is returned and the old state and pin are put back.
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "guest_start_failed"
    assert state.removed == [True]
    assert state.pins == [("unpin", PINNED), ("repin", PINNED)]
    assert state.runtime.committed.openclaw_image_id == PINNED
    assert (agent_dir / "state.txt").read_text() == "before"
    assert not snapshot_path(agent_dir, recovery_snapshot_id(agent.id)).exists()
