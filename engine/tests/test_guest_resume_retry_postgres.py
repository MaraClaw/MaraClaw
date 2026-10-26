"""Real resume failures retain durable retry intent without replacing the guest."""

from dataclasses import replace
from types import SimpleNamespace
from typing import Unpack
from uuid import uuid4

import pytest
from python_on_whales.components.container.models import ContainerConfig, ContainerHostConfig, ContainerState
from python_on_whales.exceptions import DockerException

import test_agent_start_postgres as fixtures
from app.dao.agent_start_dao import agent_start_dao
from app.db.connection import DbConnection
from app.records.agent import AgentRecord
from app.records.agent_start import AgentStartOperation
from app.services import agent_start_reconciler as reconciler, guest_admission
from app.services.agent_start_store import PostgresStartStore
from app.services.guest_admission import GuestStartPreflightError
from app.services.guest_identity import AGENT_LABEL, guest_container_name
from builtin_keyword_fakes import IncludeAllKeyword
from test_agent_start_retry import due_now, retry_metadata
from test_guest_start_safety import Docker
from test_openclaw_update_postgres import agent, member, tenant

db = fixtures.db
start_db = fixtures.start_db
guest_surface = fixtures.guest_surface


class Guest(SimpleNamespace):
    state: ContainerState
    config: ContainerConfig
    host_config: ContainerHostConfig


@pytest.fixture
async def resume_operation(
    start_db: DbConnection, guest_surface: tuple[Docker, list[str]], monkeypatch: pytest.MonkeyPatch
) -> tuple[AgentStartOperation, AgentRecord, Guest]:
    company = await tenant(start_db)
    creator = await member(start_db, company)
    identifier = await agent(start_db, company, creator, status="stopped", container="same-owned-id")
    await start_db.execute(
        "UPDATE agents SET container_port=18888,openclaw_image_id='pinned-image' WHERE id=%(id)s", {"id": identifier}
    )
    pending = await PostgresStartStore().begin(identifier)
    assert pending is not None
    docker, _probes = guest_surface
    guest = Guest(
        id="same-owned-id",
        name=guest_container_name(identifier),
        state=ContainerState(status="exited"),
        config=ContainerConfig(labels={AGENT_LABEL: str(identifier)}),
        host_config=ContainerHostConfig(memory=20, port_bindings={}),
    )
    docker.guests["same-owned-id"] = guest
    monkeypatch.setattr(guest_admission.settings, "OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES", 40)
    return *pending, guest


@pytest.mark.parametrize("state", ["exited", "created"])
async def test_transient_resume_when_due_recovers_same_operation_and_container(
    start_db: DbConnection,
    guest_surface: tuple[Docker, list[str]],
    resume_operation: tuple[AgentStartOperation, AgentRecord, Guest],
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    # Given a recorded owned guest whose first real resume will fail at Docker start.
    operation, original, guest = resume_operation
    docker, probes = guest_surface
    guest.state.status = state
    directory = reconciler.agent_manager._agent_dir(original.id)
    directory.mkdir()
    config = directory / "openclaw.json"
    config.write_bytes(b"retained configuration")
    starts: list[str] = []

    def start(reference: str) -> None:
        starts.append(reference)
        if len(starts) == 1:
            raise DockerException(["docker", "start", reference], 1, stderr=b"private daemon context")
        assert reference == "same-owned-id"
        guest.state.status = "running"

    monkeypatch.setattr(docker, "start", start)
    # When execution fails, an immediate scan runs, and database time then makes the retry due.
    with pytest.raises(GuestStartPreflightError):
        await reconciler.execute_start(operation, original)
    failed = await retry_metadata(start_db, operation)
    assert (failed.attempts, failed.last_failure_code) == (1, "runtime_failed")
    assert failed.next_retry_at is not None
    assert failed.last_attempt_at is not None
    assert (failed.next_retry_at - failed.last_attempt_at).total_seconds() == 10
    assert await agent_start_dao.get(start_db, original.id) == operation
    await reconciler.reconcile_agent_starts_once()
    assert starts == ["same-owned-id"]
    assert probes == []
    assert guest.state.status == state
    await due_now(start_db, operation)
    await reconciler.reconcile_agent_starts_once()
    # Then the same pending identity publishes the same ready guest with its history retained.
    assert await agent_start_dao.get(start_db, original.id) == replace(operation, state="succeeded")
    completed = await retry_metadata(start_db, operation)
    assert (completed.attempts, completed.next_retry_at) == (2, None)
    assert await start_db.fetchone(
        "SELECT status,container_id,container_port,openclaw_image_id FROM agents WHERE id=%(id)s", {"id": original.id}
    ) == {
        "status": "running",
        "container_id": "same-owned-id",
        "container_port": 18888,
        "openclaw_image_id": "pinned-image",
    }
    assert starts == probes == ["same-owned-id"] * 2
    assert guest.host_config.memory == 20
    assert config.read_bytes() == b"retained configuration"
    assert docker.runs == docker.removed == []


async def test_transient_admission_inventory_when_due_recovers_existing_guest(
    start_db: DbConnection,
    guest_surface: tuple[Docker, list[str]],
    resume_operation: tuple[AgentStartOperation, AgentRecord, Guest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given real resume admission whose Docker inventory fails once before starting the guest.
    operation, original, guest = resume_operation
    docker, probes = guest_surface
    inventory = docker.list
    calls: list[int] = []

    def list_guests(*, filters: list[tuple[str, str]], **kwargs: Unpack[IncludeAllKeyword]) -> list[SimpleNamespace]:
        calls.append(1)
        if len(calls) == 1:
            raise DockerException(["docker", "ps"], 1, stderr=b"private daemon context")
        return inventory(filters=filters, **kwargs)

    monkeypatch.setattr(docker, "list", list_guests)
    # When the worker fails admission, skips the future reservation, then automatically retries when due.
    await reconciler.reconcile_agent_starts_once()
    failed = await retry_metadata(start_db, operation)
    assert (failed.attempts, failed.last_failure_code) == (1, "runtime_failed")
    assert failed.next_retry_at is not None
    await reconciler.reconcile_agent_starts_once()
    assert len(calls) == 1
    assert probes == []
    await due_now(start_db, operation)
    await reconciler.reconcile_agent_starts_once()
    # Then admission and publication succeed on the original operation and guest.
    assert await agent_start_dao.get(start_db, original.id) == replace(operation, state="succeeded")
    assert probes == ["same-owned-id"] * 2
    assert (await retry_metadata(start_db, operation)).attempts == 2
    assert guest.state.status == "running"
    assert docker.runs == docker.removed == []


@pytest.mark.parametrize("foreign_owner", [False, True], ids=["configuration", "ownership"])
async def test_real_resume_refusals_when_scanned_remain_intervention_only(
    start_db: DbConnection,
    guest_surface: tuple[Docker, list[str]],
    resume_operation: tuple[AgentStartOperation, AgentRecord, Guest],
    foreign_owner: bool,
) -> None:
    # Given an owned but non-resumable guest, or a reference owned by another agent.
    operation, original, guest = resume_operation
    docker, probes = guest_surface
    if foreign_owner:
        guest.config.labels = {AGENT_LABEL: str(uuid4())}
    else:
        guest.state.status = "paused"
    # When two worker scans encounter the real typed refusal.
    await reconciler.reconcile_agent_starts_once()
    await reconciler.reconcile_agent_starts_once()
    # Then the refused operation remains pending with no retry schedule or runtime mutation.
    metadata = await retry_metadata(start_db, operation)
    code = "ownership_conflict" if foreign_owner else "configuration_failed"
    assert (metadata.attempts, metadata.last_failure_code, metadata.next_retry_at) == (1, code, None)
    assert await agent_start_dao.get(start_db, original.id) == operation
    assert await start_db.fetchval("SELECT status FROM agents WHERE id=%(id)s", {"id": original.id}) == "stopped"
    assert probes == docker.runs == docker.removed == []
