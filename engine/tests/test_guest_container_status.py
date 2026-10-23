"""Read-only guest diagnostics at the existing advanced metrics surface."""

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from threading import get_ident
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import TypeAdapter
from python_on_whales import ClientNotFoundError, DockerClient
from python_on_whales.components.container import cli_wrapper
from python_on_whales.exceptions import DockerException, NoSuchContainer

from app.api import advanced
from app.core.json_types import JsonObject, json_object_from
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.services.agent_manager import AgentManager, agent_manager
from app.services.guest_container_status import get_guest_container_status


@pytest.fixture
def agent() -> AgentRecord:
    return AgentRecord(
        UUID("12345678-1234-4321-8765-123456789abc"),
        "Worker",
        uuid4(),
        status="stopped",
        container_id="container-id",
        container_port=19876,
    )


@pytest.fixture
def inspect_payload(agent: AgentRecord) -> JsonObject:
    return {
        "Id": "container-id",
        "Name": "/maraclaw-agent-12345678",
        "Created": "2026-01-02T03:04:05Z",
        "RestartCount": 3,
        "State": {
            "Running": True,
            "Status": "running",
            "OOMKilled": True,
            "Health": {"Status": "unhealthy", "FailingStreak": 2, "Log": [{"Output": "private health output"}]},
        },
        "Config": {
            "Labels": {"maraclaw.agent_id": str(agent.id), "private.label": "private label"},
            "Env": ["TOKEN=private environment"],
        },
        "HostConfig": {
            "Memory": 1073741824,
            "MemorySwap": 2147483648,
            "NanoCpus": 1500000000,
            "CpuQuota": 75000,
            "CpuPeriod": 100000,
            "CpusetCpus": "0-1",
            "PidsLimit": 256,
            "PortBindings": {"18789/tcp": [{"HostPort": "44444"}]},
        },
        "NetworkSettings": {
            "Ports": {
                "18789/tcp": [
                    {"HostIp": "127.0.0.1", "HostPort": "19876"},
                    {"HostIp": "::1", "HostPort": "19876"},
                ],
                "8080/tcp": None,
            }
        },
    }


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch, inspect_payload: JsonObject) -> AgentManager:
    def inspect_command(_command: list[str]) -> str:
        assert _command[-3:] == ["container", "inspect", "container-id"]
        return json.dumps([inspect_payload])

    monkeypatch.setattr(cli_wrapper, "run", inspect_command)
    instance = AgentManager.__new__(AgentManager)
    instance.docker_client = DockerClient()
    return instance


def test_observed_status_when_legacy_name_has_full_ownership(agent: AgentRecord, manager: AgentManager) -> None:
    # Given a legacy-named guest whose full ownership label matches.
    # When diagnostics inspect the guest.
    result = get_guest_container_status(manager, agent)
    # Then observations, not persisted status or desired settings, are reported.
    assert result == {
        "running": True,
        "status": "running",
        "ports": {
            "18789/tcp": [
                {"HostIp": "127.0.0.1", "HostPort": "19876"},
                {"HostIp": "::1", "HostPort": "19876"},
            ],
            "8080/tcp": None,
        },
        "created": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "ownership": {"agent_id": str(agent.id), "matches": True},
        "name": "maraclaw-agent-12345678",
        "expected_name": f"maraclaw-agent-{agent.id}",
        "restart_count": 3,
        "health": {"status": "unhealthy", "failing_streak": 2},
        "oom_killed": True,
        "limits": {
            "memory_bytes": 1073741824,
            "memory_swap_bytes": 2147483648,
            "nano_cpus": 1500000000,
            "cpu_quota": 75000,
            "cpu_period": 100000,
            "cpuset_cpus": "0-1",
            "pids_limit": 256,
        },
        "port_drift": {"persisted_port": 19876, "published_ports": [19876], "drifted": False},
    }


@pytest.mark.parametrize("owner", [None, "12345678", "12345678-1234-4321-8765-000000000000"])
def test_ownership_mismatch_when_label_is_absent_short_or_foreign(
    agent: AgentRecord, manager: AgentManager, inspect_payload: JsonObject, owner: str | None
) -> None:
    # Given a matching name but no matching full UUID label.
    inspect_payload["Name"] = f"/maraclaw-agent-{agent.id}"
    inspect_payload["Config"] = {"Labels": {} if owner is None else {"maraclaw.agent_id": owner}}
    # When the guest is observed.
    result = get_guest_container_status(manager, agent)
    # Then the name is never accepted as proof of ownership.
    assert result.get("ownership") == {"agent_id": owner, "matches": False}


@pytest.mark.parametrize(
    ("ports", "persisted", "published", "drifted"),
    [
        ({"18789/tcp": [{"HostPort": "23456"}], "8080/tcp": [{"HostPort": "19876"}]}, 19876, [23456], True),
        ({"18789/tcp": None}, 19876, [], True),
        ({"18789/tcp": [{"HostPort": "19876"}, {"HostPort": "23456"}]}, 19876, [19876, 23456], True),
        ({"18789/tcp": [{"HostPort": "23456"}]}, None, [23456], None),
        (None, 19876, [], None),
    ],
)
def test_port_drift_when_published_gateway_differs(
    agent: AgentRecord,
    manager: AgentManager,
    inspect_payload: JsonObject,
    ports: JsonObject | None,
    persisted: int | None,
    published: list[int],
    drifted: bool | None,
) -> None:
    # Given observed bindings independent of HostConfig requests and persisted ports.
    agent.container_port = persisted
    inspect_payload["NetworkSettings"] = {"Ports": ports}
    # When diagnostics compare the gateway binding.
    result = get_guest_container_status(manager, agent)
    # Then only actual gateway publications are compared, with unknowns kept unknown.
    assert result.get("port_drift") == {"persisted_port": persisted, "published_ports": published, "drifted": drifted}


def test_optional_health_and_unlimited_values_when_inspect_has_no_healthcheck(
    agent: AgentRecord, manager: AgentManager, inspect_payload: JsonObject
) -> None:
    # Given a guest without a healthcheck and with unlimited hard limits.
    inspect_payload["State"] = {"Running": False, "Status": "exited", "OOMKilled": False}
    inspect_payload["HostConfig"] = {"Memory": 0, "NanoCpus": 0, "CpuQuota": -1, "PidsLimit": -1}
    # When diagnostics observe its limits.
    result = get_guest_container_status(manager, agent)
    # Then health is omitted and zero/-1 are not replaced by configured defaults.
    assert "health" not in result
    assert result.get("limits") == {
        "memory_bytes": 0,
        "memory_swap_bytes": None,
        "nano_cpus": 0,
        "cpu_quota": -1,
        "cpu_period": None,
        "cpuset_cpus": None,
        "pids_limit": -1,
    }


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (NoSuchContainer(["docker", "inspect"], 1), "not_found"),
        (DockerException(["docker"], 1), "error"),
        (ClientNotFoundError("docker"), "error"),
    ],
)
def test_compatible_status_when_inspect_fails(
    agent: AgentRecord, manager: AgentManager, monkeypatch: pytest.MonkeyPatch, failure: Exception, status: str
) -> None:
    # Given an inspect failure, including lazy SDK property access.
    def fail_inspect(_command: list[str]) -> str:
        raise failure

    monkeypatch.setattr(cli_wrapper, "run", fail_inspect)
    # When diagnostics inspect the guest.
    result = get_guest_container_status(manager, agent)
    # Then the existing two-field error contract is unchanged.
    assert result == {"running": False, "status": status}


@pytest.mark.parametrize("container_id", [None, ""])
def test_compatible_status_when_container_is_unassigned(
    agent: AgentRecord, manager: AgentManager, monkeypatch: pytest.MonkeyPatch, container_id: str | None
) -> None:
    # Given no assigned container.
    agent.container_id = container_id

    def unexpected_inspect(_command: list[str]) -> str:
        pytest.fail("Unassigned containers must not be inspected")

    monkeypatch.setattr(cli_wrapper, "run", unexpected_inspect)
    # When diagnostics are requested.
    result = get_guest_container_status(manager, agent)
    # Then persisted status is returned without inspect fields.
    assert result == {"running": False, "status": "stopped"}


def test_compatible_status_when_docker_is_unavailable(agent: AgentRecord, manager: AgentManager) -> None:
    # Given an assigned guest with no Docker client.
    manager.docker_client = None
    # When diagnostics are requested.
    result = get_guest_container_status(manager, agent)
    # Then existing unavailable-client behavior remains unchanged.
    assert result == {"running": False, "status": "stopped"}


async def test_metrics_inspect_runs_in_worker_thread_and_preserves_safe_diagnostics(
    agent: AgentRecord, manager: AgentManager, inspect_payload: JsonObject, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the existing metrics route with fake persistence and a real SDK parser.
    event_loop_thread = get_ident()
    inspect_threads: list[int] = []

    def inspect_command(_command: list[str]) -> str:
        assert _command[-3:] == ["container", "inspect", "container-id"]
        inspect_threads.append(get_ident())
        return json.dumps([inspect_payload])

    monkeypatch.setattr(cli_wrapper, "run", inspect_command)
    monkeypatch.setattr(agent_manager, "docker_client", manager.docker_client)
    monkeypatch.setattr(advanced, "check_agent_access", AsyncMock(return_value=(agent, "use")))
    monkeypatch.setattr(advanced.task_dao, "count_for_agent", AsyncMock(return_value=0))

    class Connection:
        async def fetchval(self, _sql: str, _params: JsonObject) -> int:
            return 0

    @asynccontextmanager
    async def connection_ctx() -> AsyncGenerator[Connection]:
        yield Connection()

    monkeypatch.setattr(advanced, "connection_ctx", connection_ctx)
    app = FastAPI()
    app.include_router(advanced.router, prefix="/api")
    app.dependency_overrides[advanced.get_current_user] = lambda: UserRecord(agent.creator_id)
    # When the existing HTTP status surface is requested.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/agents/{agent.id}/metrics")
    # Then inspection runs exactly once off-loop and only allowlisted details reach the wire.
    assert response.status_code == 200
    assert len(inspect_threads) == 1
    assert inspect_threads[0] != event_loop_thread
    container = json_object_from(TypeAdapter(JsonObject).validate_json(response.content)["container"])
    assert container["ownership"] == {"agent_id": str(agent.id), "matches": True}
    assert container["created"] == "2026-01-02T03:04:05+00:00"
    assert "private" not in response.text
