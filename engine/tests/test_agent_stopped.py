import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from pywhales.exceptions import NoSuchContainer
from redis.exceptions import RedisError

from app.services import (
    agent_lifecycle as lifecycle,
    agent_lifecycle_events as lifecycle_events,
    agent_manager as mgr,
    openclaw_update_exclusion as exclusion,
)
from app.services.guest_identity import GuestOwnershipError
from test_agent_lifecycle_http import surface  # noqa: F401


@pytest.fixture
def docker(surface, monkeypatch):  # noqa: F811
    monkeypatch.setattr(lifecycle.agent_manager, "docker_client", object())
    return surface


async def post_stop(app, agent):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(f"/api/agents/{agent.id}/stop")


def docker_stop(monkeypatch, outcome):
    """Drive the real manager stop with a Docker-level outcome: found/missing/no_such/ownership."""

    def stop(_client, agent, **_kwargs):
        if outcome == "no_such":
            raise NoSuchContainer(["docker", "stop"], 1)
        if outcome == "ownership":
            raise GuestOwnershipError(agent.id, "old", str(uuid4()))
        return outcome != "missing"

    monkeypatch.setattr(mgr, "stop_guest_containers", stop)


def events_of(runtime):
    return [json.loads(message) for _channel, message in runtime.published]


async def test_stop_publishes_after_commit_and_before_unlock(docker, monkeypatch):
    # Given a configured Docker client and a confirmed stop.
    app, runtime, agent = docker
    docker_stop(monkeypatch, "found")
    before = datetime.now(UTC)
    # When the user stops the agent over HTTP.
    response = await post_stop(app, agent)
    after = datetime.now(UTC)
    # Then the frozen payload goes out on agent_lifecycle only after the committed stop, under the lock.
    assert response.status_code == 200
    release_at = runtime.events.index("release")
    assert runtime.events[release_at - 3 : release_at] == ["persist", "commit_locked", "publish"]
    assert [channel for channel, _ in runtime.published] == ["agent_lifecycle"]
    [payload] = events_of(runtime)
    stopped_at = datetime.fromisoformat(payload.pop("stopped_at"))
    assert payload == {"version": 1, "event": "AgentStopped", "agent_id": str(agent.id), "container_id": "old"}
    assert stopped_at.utcoffset() == timedelta(0)
    assert before <= stopped_at <= after
    assert runtime.committed.status == "stopped"


async def test_failed_stop_does_not_publish(docker, monkeypatch):
    # Given Docker cannot confirm the stop.
    app, runtime, agent = docker

    async def refuse(_record):
        return False

    monkeypatch.setattr(lifecycle.agent_manager, "stop_container", refuse)
    # When the user stops the agent.
    response = await post_stop(app, agent)
    # Then the retained STOPPED commit still returns 502 and nothing is announced.
    assert response.status_code == 502
    assert runtime.committed.status == "stopped"
    assert runtime.published == []


async def test_retry_after_failed_stop_publishes_despite_committed_stopped(docker, monkeypatch):
    # Given a failed stop already committed STOPPED with the container ID retained.
    app, runtime, agent = docker
    results = iter([False, True])

    async def perform(record):
        record.status = "stopped"
        return next(results)

    monkeypatch.setattr(lifecycle.agent_manager, "stop_container", perform)
    first = await post_stop(app, agent)
    assert (first.status_code, runtime.committed.status, runtime.published) == (502, "stopped", [])
    # When the user retries and Docker now confirms.
    second = await post_stop(app, agent)
    # Then the prior STOPPED status does not suppress the notification.
    assert second.status_code == 200
    assert [event["container_id"] for event in events_of(runtime)] == ["old"]


@pytest.mark.parametrize(("outcome", "container_id"), [("found", "old"), ("missing", None), ("no_such", None)])
async def test_docker_confirmed_stop_publishes_retained_or_cleared_container(
    docker, monkeypatch, outcome, container_id
):
    # Given Docker confirms the guest stopped, already stopped, or absent.
    app, runtime, agent = docker
    docker_stop(monkeypatch, outcome)
    # When the user stops the agent through the real manager.
    response = await post_stop(app, agent)
    # Then the event carries the container reference that was actually committed.
    assert response.status_code == 200
    assert runtime.committed.container_id == container_id
    assert [event["container_id"] for event in events_of(runtime)] == [container_id]


async def test_repeated_successful_stops_each_publish(docker, monkeypatch):
    # Given two successful user stop requests for the same agent.
    app, runtime, agent = docker
    docker_stop(monkeypatch, "found")
    # When both complete.
    codes = [(await post_stop(app, agent)).status_code for _ in range(2)]
    # Then at-most-once Pub/Sub still sees one event per request; consumers must tolerate duplicates.
    assert codes == [200, 200]
    assert len(runtime.published) == 2


async def test_no_docker_stop_keeps_http_contract_without_event(surface):  # noqa: F811
    # Given the deliberate Docker-unavailable mode.
    app, runtime, agent = surface
    # When the user stops the agent.
    response = await post_stop(app, agent)
    # Then the compatibility stop succeeds and nothing is announced.
    assert response.status_code == 200
    assert runtime.committed.status == "stopped"
    assert runtime.published == []


async def test_ownership_refusal_does_not_publish(docker, monkeypatch):
    # Given the guest belongs to another agent.
    app, runtime, agent = docker
    docker_stop(monkeypatch, "ownership")
    # When the user stops the agent.
    response = await post_stop(app, agent)
    # Then the refusal is a 502 with no announcement.
    assert response.status_code == 502
    assert runtime.published == []


async def test_upgrade_gate_refusal_does_not_publish(docker, monkeypatch):
    # Given an active OpenClaw upgrade holds the agent.
    app, runtime, agent = docker

    async def refuse(_id):
        raise HTTPException(status_code=423, detail="upgrade running")

    docker_stop(monkeypatch, "found")
    monkeypatch.setattr(exclusion, "assert_no_active_run", refuse)
    # When the user stops the agent.
    response = await post_stop(app, agent)
    # Then the gate response is returned untouched with no announcement.
    assert response.status_code == 423
    assert runtime.published == []


@pytest.mark.parametrize("failing", ["persist", "commit"])
async def test_persistence_failure_does_not_publish(docker, monkeypatch, failing):
    # Given Docker confirmed the stop but the runtime cannot be made durable.
    app, runtime, agent = docker
    docker_stop(monkeypatch, "found")

    async def broken(*_args, **_kwargs):
        raise RuntimeError("db down")

    if failing == "persist":
        monkeypatch.setattr(lifecycle.agent_dao, "update", broken)
    else:
        monkeypatch.setattr(runtime, "commit", broken)
    # When the user stops the agent.
    with pytest.raises(RuntimeError, match="db down"):
        await post_stop(app, agent)
    # Then no event claims a stop that was not committed.
    assert runtime.published == []


@pytest.mark.parametrize("error", [RedisError("down"), ConnectionRefusedError("refused")])
async def test_notification_outage_keeps_success_response(docker, monkeypatch, error):
    # Given the stop commits but Redis rejects the publish.
    app, runtime, agent = docker
    docker_stop(monkeypatch, "found")
    warnings = []
    monkeypatch.setattr(lifecycle_events, "logger", SimpleNamespace(warning=lambda *args: warnings.append(args)))

    async def outage(*_args):
        raise error

    monkeypatch.setattr(runtime, "publish", outage)
    # When the user stops the agent.
    response = await post_stop(app, agent)
    # Then the HTTP result is unchanged and the failure is only logged.
    assert response.status_code == 200
    assert response.json()["status"] == runtime.committed.status == "stopped"
    assert [(args[0], args[1]) for args in warnings] == [
        ("AgentStopped notification not published for agent {}: {}", agent.id)
    ]
