"""Ordinary Start must not be a destructive configuration replacement."""

from dataclasses import replace
from uuid import uuid4

import pytest

import test_guest_start_safety as start_fixtures
from app.services import guest_readiness, guest_start
from app.services.guest_admission import GuestStartPreflightError
from app.services.guest_identity import AGENT_LABEL, GuestOwnershipError

env = start_fixtures.env


@pytest.fixture(autouse=True)
def ready_guest(env, monkeypatch):
    _manager, docker, _agent = env
    docker.probes = []
    docker.starts = []
    monkeypatch.setattr(guest_readiness, "READY_INTERVAL_SECONDS", 0)

    def probe(_client, reference, timeout):
        docker.probes.append(reference)
        assert timeout > 0
        return True

    def start(reference):
        docker.starts.append(reference)
        docker.inspect(reference).state.status = "running"

    monkeypatch.setattr(guest_readiness, "probe_guest", probe)
    monkeypatch.setattr(docker, "start", start, raising=False)


async def test_repeated_start_adopts_ready_guest_without_disk_writes(env, monkeypatch):
    # Given an owned running guest and a workspace that must not be rewritten.
    manager, docker, agent = env

    async def forbidden(_id):
        pytest.fail("adoption rewrote the workspace")

    monkeypatch.setattr(manager, "_materialize_agent_dir", forbidden)
    # When the same guest is started repeatedly.
    results = [await manager.start_container(agent), await manager.start_container(agent)]
    # Then both requests adopt it after two independent successful probes.
    assert results == ["old", "old"]
    assert docker.probes == ["old"] * 4
    assert docker.runs == docker.removed == docker.starts == []


async def test_stopped_guest_resumes_same_id(env):
    # Given an owned stopped guest.
    manager, docker, agent = env
    docker.guests["old"].state.status = "exited"
    agent.status = "stopped"
    # When Start resumes it.
    result = await manager.start_container(agent)
    # Then its identity and mounted files survive.
    assert result == "old"
    assert docker.starts == ["old"]
    assert docker.runs == docker.removed == []


@pytest.mark.parametrize("status", ["running", "exited"])
async def test_existing_guest_registers_before_reuse(env, status):
    manager, docker, agent = env
    docker.guests["old"].state.status = status
    await manager.start_container(agent)
    manager._prepare_linkup_proxy_token.assert_awaited_once_with(agent.id)
    assert docker.runs == docker.removed == []


@pytest.mark.parametrize("status", ["running", "exited"])
async def test_existing_guest_is_not_reused_when_registration_fails(env, status):
    manager, docker, agent = env
    docker.guests["old"].state.status = status
    manager._prepare_linkup_proxy_token.side_effect = RuntimeError("registration failed")
    with pytest.raises(RuntimeError, match="registration failed"):
        await manager.start_container(agent)
    assert docker.starts == docker.probes == docker.runs == docker.removed == []


async def test_stale_record_adopts_canonical_guest(env):
    # Given Docker created a guest before its reference was committed.
    manager, docker, agent = env
    agent.container_id = "missing"
    # When replay discovers the canonical exact-label-owned candidate.
    result = await manager.start_container(agent)
    # Then no second writer is created.
    assert result == "old"
    assert docker.runs == docker.removed == []


async def test_multiple_owned_candidates_fail_before_workspace_writes(env, monkeypatch):
    # Given a recorded old guest and a distinct canonical candidate.
    manager, docker, agent = env
    docker.guests["old"].name = "parked"
    docker.add(replace(agent, container_id="second"))

    async def forbidden(_id):
        pytest.fail("conflict rewrote the workspace")

    monkeypatch.setattr(manager, "_materialize_agent_dir", forbidden)
    # When ordinary Start cannot choose a unique writer.
    with pytest.raises(GuestStartPreflightError):
        await manager.start_container(agent)
    # Then neither owned guest is destroyed.
    assert set(docker.guests) == {"old", "second"}
    assert docker.removed == docker.runs == []


async def test_foreign_canonical_candidate_fails_before_writes(env, monkeypatch):
    # Given an unrecorded canonical name owned by another agent.
    manager, docker, agent = env
    agent.container_id = None
    docker.guests["old"].config.labels[AGENT_LABEL] = str(uuid4())

    async def forbidden(_id):
        pytest.fail("foreign ownership rewrote the workspace")

    monkeypatch.setattr(manager, "_materialize_agent_dir", forbidden)
    # When Start inspects the canonical name.
    with pytest.raises(GuestOwnershipError):
        await manager.start_container(agent)
    # Then the foreign guest remains untouched.
    assert docker.removed == docker.runs == docker.starts == []


async def test_absent_guest_is_created_with_replay_label(env):
    # Given no guest and an already persisted operation identity.
    manager, docker, agent = env
    docker.guests.clear()
    agent.container_id = None
    operation = uuid4()
    token = guest_start.start_operation_id.set(operation)
    # When Start creates its only candidate.
    try:
        result = await manager.start_container(agent)
    finally:
        guest_start.start_operation_id.reset(token)
    # Then it is discoverable after a crash and ready before returning.
    assert result == "created-1"
    assert docker.runs[0]["labels"]["maraclaw.start_operation"] == str(operation)
    assert docker.probes == [result, result]


async def test_ready_passes_must_be_consecutive(env, monkeypatch):
    # Given a guest that briefly loses readiness between successful probes.
    manager, _docker, agent = env
    outcomes = iter((True, False, True, True))
    probes = []

    def probe(_client, reference, timeout):
        probes.append(reference)
        assert timeout > 0
        return next(outcomes)

    monkeypatch.setattr(guest_readiness, "probe_guest", probe)
    # When Start waits for readiness.
    result = await manager.start_container(agent)
    # Then the failure resets the consecutive-pass count.
    assert result == "old"
    assert probes == ["old"] * 4


@pytest.mark.parametrize("existing", [True, False])
async def test_unready_guest_is_preserved_and_new_writer_quiesced(env, monkeypatch, existing):
    # Given a guest that cannot pass readiness before its deadline.
    manager, docker, agent = env
    if not existing:
        docker.guests.clear()
        agent.container_id = None

    async def unready(_client, _reference):
        return False

    monkeypatch.setattr(guest_start, "guest_ready", unready)
    # When readiness fails.
    with pytest.raises(GuestStartPreflightError) as failure:
        await manager.start_container(agent)
    # Then existing guests survive, while a newly created writer is stopped, never orphaned.
    assert failure.value.code == "readiness_failed"
    reference = "old" if existing else "created-1"
    assert docker.guests[reference].state.status == ("running" if existing else "exited")
    assert docker.removed == []


async def test_ready_probe_cannot_succeed_after_deadline(env, monkeypatch):
    # Given two passes whose second completion exceeds the bounded deadline.
    _manager, docker, _agent = env
    ticks = iter((0.0, 0.0, 0.1, 0.2, 121.0, 121.0, 121.0))
    monkeypatch.setattr(guest_readiness, "monotonic", lambda: next(ticks))
    # When the readiness deadline expires during the second probe.
    result = await guest_readiness.guest_ready(docker, "old")
    # Then late completion is not readiness publication permission.
    assert result is False
