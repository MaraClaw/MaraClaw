from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from httpx import ASGITransport, AsyncClient
from scripts.api_contract_document import mapping
from scripts.export_api_contracts import export_contract

import test_openclaw_update_postgres as postgres
from app.core.security import create_access_token
from app.db.connection import DbConnection
from app.db.errors import CheckViolationError, ForeignKeyViolationError
from app.db.session import bind_crud_connection
from app.main import app
from app.schemas.agent_lifecycle import AgentLifecycleOut
from app.schemas.agent_setup_mcp import AgentSetupMCPStatus
from app.services.agent_manager import agent_manager

db = postgres.db


@pytest.fixture
async def lifecycle_surface(
    db: DbConnection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[tuple[AsyncClient, UUID]]:
    company = await postgres.tenant(db)
    owner = await postgres.member(db, company)
    agent_id = await postgres.agent(db, company, owner, status="stopped", container=None)
    await db.execute(
        "INSERT INTO agent_setups(agent_id,inputs,state) VALUES "
        "(%(id)s,'{\"gateway_key_encrypted\":\"private-replay-input\"}','succeeded')",
        {"id": agent_id},
    )
    await db.execute(
        "INSERT INTO agent_setup_mcp_work(agent_id,server_id,owner_id,tenant_id,requester_id,"
        "state,warning_code,attempts) VALUES "
        "(%(id)s,'vendor/qa',%(owner)s,%(company)s,%(owner)s,'warning','connection_failed',2)",
        {"id": agent_id, "owner": owner, "company": company},
    )
    monkeypatch.setattr(agent_manager, "_agent_dir", lambda identifier: tmp_path / str(identifier))
    token = await create_access_token(str(owner), "member")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://lifecycle-integration",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client, agent_id


def test_lifecycle_contract_when_application_is_exported_includes_typed_dependency_closure() -> None:
    document = export_contract(app)
    declared = mapping(document["x-maraclaw-contract"])["schemas"]
    assert isinstance(declared, list)
    assert all(
        name in declared
        for name in (
            "AgentLifecycleOut",
            "AgentLifecycleRuntimeOut",
            "AgentLifecycleStartOut",
            "AgentLifecycleAvailabilityOut",
            "AgentSetupStatus",
            "AgentSetupMCPStatus",
            "AgentSetupMCPRetry",
            "AgentUpdateOut",
        )
    )
    schemas = mapping(mapping(document["components"])["schemas"])
    assert "api_key_hash" not in mapping(mapping(schemas["AgentOut"])["properties"])
    assert "access_reason" in mapping(mapping(schemas["AgentOut"])["properties"])


def test_lifecycle_routes_when_mounted_keep_service_owned_transaction_boundaries() -> None:
    expected = {"/api/agents/{agent_id}/lifecycle", "/api/agents/{agent_id}/setup/mcp-retry"}
    mounted = [context for context in iter_route_contexts(app.routes) if context.path_format in expected]
    assert {context.path_format for context in mounted} == expected
    for context in mounted:
        assert isinstance(context.route, APIRoute)
        assert bind_crud_connection not in {dependency.call for dependency in context.route.dependant.dependencies}


async def test_lifecycle_when_called_on_bootstrapped_application_reads_safe_recorded_state(
    lifecycle_surface: tuple[AsyncClient, UUID],
) -> None:
    client, agent_id = lifecycle_surface
    response = await client.get(f"/api/agents/{agent_id}/lifecycle")
    assert response.status_code == 200
    result = AgentLifecycleOut.model_validate_json(response.content)
    assert result.runtime.recorded_status == "stopped"
    assert result.setup.state == "succeeded"
    assert result.optional_mcp[0].server_id == "vendor/qa"
    assert result.optional_mcp[0].retryable
    assert result.availability.can_retry_mcp
    assert "private-replay-input" not in response.text
    assert "api_key_hash" not in response.text


async def test_mcp_retry_when_called_on_bootstrapped_application_commits_selected_work(
    lifecycle_surface: tuple[AsyncClient, UUID], db: DbConnection
) -> None:
    client, agent_id = lifecycle_surface
    response = await client.post(f"/api/agents/{agent_id}/setup/mcp-retry", json={"server_id": "vendor/qa"})
    assert response.status_code == 202
    result = AgentSetupMCPStatus.model_validate_json(response.content)
    assert (result.server_id, result.state, result.attempts, result.warning_code) == ("vendor/qa", "pending", 2, None)
    assert await db.fetchone(
        "SELECT state,warning_code,attempts FROM agent_setup_mcp_work WHERE agent_id=%(id)s",
        {"id": agent_id},
    ) == {"state": "pending", "warning_code": None, "attempts": 2}


@pytest.mark.parametrize("suffix", ["lifecycle", "setup/mcp-retry"])
async def test_lifecycle_routes_when_token_is_absent_require_authentication(suffix: str) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://lifecycle-integration") as client:
        response = await client.request(
            "GET" if suffix == "lifecycle" else "POST",
            f"/api/agents/{uuid4()}/{suffix}",
            json={"server_id": "vendor/qa"} if suffix != "lifecycle" else None,
        )
    assert response.status_code == 401


@pytest.mark.parametrize("server", ["unknown/server", "vendor/qa"])
async def test_mcp_retry_when_server_is_unknown_or_terminal_returns_typed_refusal(
    lifecycle_surface: tuple[AsyncClient, UUID], db: DbConnection, server: str
) -> None:
    client, agent_id = lifecycle_surface
    await db.execute(
        "UPDATE agent_setup_mcp_work SET state='succeeded',warning_code=NULL WHERE agent_id=%(id)s", {"id": agent_id}
    )
    response = await client.post(f"/api/agents/{agent_id}/setup/mcp-retry", json={"server_id": server})
    assert (response.status_code, response.json()["detail"]["code"]) == (
        (404, "agent_setup_mcp_unknown_server")
        if server == "unknown/server"
        else (409, "agent_setup_mcp_not_retryable")
    )


@pytest.mark.parametrize("transition", ["succeeded", "cancelled", "replaced", "deleted"])
async def test_bootstrap_when_start_operation_changes_clears_or_prunes_retry_schedule(
    db: DbConnection, transition: str
) -> None:
    agent_id, operation_id = uuid4(), uuid4()
    await db.execute(
        "INSERT INTO agent_start_operations(agent_id,operation_id,state,creator_id) "
        "VALUES (%(agent)s,%(operation)s,'pending',%(creator)s)",
        {"agent": agent_id, "operation": operation_id, "creator": uuid4()},
    )
    await db.execute(
        "INSERT INTO agent_start_retry_metadata(operation_id,attempts,next_retry_at) VALUES (%(id)s,2,now())",
        {"id": operation_id},
    )
    statements = {
        "succeeded": "UPDATE agent_start_operations SET state='succeeded' WHERE agent_id=%(id)s",
        "cancelled": "UPDATE agent_start_operations SET state='cancelled' WHERE agent_id=%(id)s",
        "replaced": "UPDATE agent_start_operations SET operation_id=gen_random_uuid() WHERE agent_id=%(id)s",
        "deleted": "DELETE FROM agent_start_operations WHERE agent_id=%(id)s",
    }
    await db.execute(statements[transition], {"id": agent_id})
    row = await db.fetchone(
        "SELECT attempts,next_retry_at FROM agent_start_retry_metadata WHERE operation_id=%(id)s", {"id": operation_id}
    )
    assert row == ({"attempts": 2, "next_retry_at": None} if transition in ("succeeded", "cancelled") else None)


@pytest.mark.parametrize("table", ["agent_start_retry_metadata", "agent_setup_mcp_work"])
async def test_bootstrap_when_retry_attempts_are_negative_rejects_row(db: DbConnection, table: str) -> None:
    statements = {
        "agent_start_retry_metadata": "INSERT INTO agent_start_retry_metadata(operation_id,attempts) VALUES (%(id)s,-1)",
        "agent_setup_mcp_work": "INSERT INTO agent_setup_mcp_work(agent_id,server_id,owner_id,requester_id,attempts) "
        "VALUES (%(id)s,'vendor/qa',%(id)s,%(id)s,-1)",
    }
    with pytest.raises(CheckViolationError):
        await db.execute(statements[table], {"id": uuid4()})


async def test_bootstrap_when_optional_work_has_no_agent_rejects_row(db: DbConnection) -> None:
    with pytest.raises(ForeignKeyViolationError):
        await db.execute(
            "INSERT INTO agent_setup_mcp_work(agent_id,server_id,owner_id,requester_id) "
            "VALUES (%(id)s,'vendor/qa',%(id)s,%(id)s)",
            {"id": uuid4()},
        )
