import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.api import background_tasks
from app.config import get_settings
from app.core.permissions import check_agent_access
from app.dao.agent_dao import agent_dao
from app.dao.company_transfer_cleanup_dao import company_transfer_cleanup_dao as cleanup
from app.records.agent import AgentRecord
from app.schemas.company_admission import PendingAdmissionResponse
from app.services import (
    company_transfer_cleanup as service,
    company_transfer_credentials as credentials,
    company_transfer_guards as guards,
    company_transfer_requests as requests,
)
from app.services.openclaw_update_gate import assert_work_allowed
from company_admission_fixtures import AdmissionScenario, admission_scenario
from test_company_transfer_postgres import submit_paid, transfer_scenario
from test_company_transfer_retention_postgres import seed_retained_data

__all__ = ["admission_scenario", "transfer_scenario"]


async def test_nullable_success_completes_real_cleanup_row(transfer_scenario: AdmissionScenario) -> None:
    # Given a durable pending row in an isolated PostgreSQL schema.
    scenario = transfer_scenario
    agent_id = await seed_retained_data(scenario)
    request_id = await submit_paid(scenario)
    await scenario.db.execute(
        "INSERT INTO company_transfer_runtime_cleanup(agent_id,transfer_request_id,choice) "
        "VALUES(%(agent)s,%(request)s,'paid')",
        {"agent": agent_id, "request": request_id},
    )
    # When the actual adapter binds the nullable successful outcome.
    await cleanup.record_attempt(agent_id, None)
    # Then completion is persisted, not rejected by PostgreSQL's parameter inference.
    row = await scenario.db.fetchone(
        "SELECT state,attempts,last_error,completed_at IS NOT NULL AS completed "
        "FROM company_transfer_runtime_cleanup WHERE agent_id=%(id)s",
        {"id": agent_id},
    )
    assert row == {"state": "completed", "attempts": 1, "last_error": None, "completed": True}
    assert not await cleanup.is_pending(agent_id)


async def test_failed_free_cleanup_retries_to_completion_without_touching_retained_history(
    transfer_scenario: AdmissionScenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given retained Postgres, filesystem and SQLite history beside old runtime credentials.
    scenario = transfer_scenario
    agent_id = await seed_retained_data(scenario)
    directory = tmp_path / str(agent_id)
    workspace = directory / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("retained memory")
    (workspace / "report.txt").write_text("retained file")
    (directory / "openclaw.json").write_text('{"env":{"SOURCE_SECRET":"old"}}')
    guest = directory / "agents/main/agent"
    guest.mkdir(parents=True)
    database = guest / "openclaw-agent.sqlite"
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("CREATE TABLE messages(content TEXT)")
        db.execute("INSERT INTO messages VALUES ('retained chat')")
        db.execute("CREATE TABLE auth_profile_store(secret TEXT)")
        db.execute("INSERT INTO auth_profile_store VALUES ('old credential')")
    secret = tmp_path / "_gogcli_secrets" / str(agent_id) / "keyring_password"
    secret.parent.mkdir(parents=True)
    secret.write_text("old keyring credential")
    monkeypatch.setattr(service.agent_manager, "_agent_dir", lambda _id: tmp_path / str(_id))
    monkeypatch.setattr(service.agent_manager, "docker_client", None)
    monkeypatch.setattr(get_settings(), "PROCESS_ROLE", "api")
    monkeypatch.setattr(guards, "get_transfer_policy", requests.get_transfer_policy)
    monkeypatch.setattr(credentials, "gogcli_secret_file", lambda _id: secret)
    original_remove = service.agent_manager.remove_container
    failed = False

    async def remove(agent: AgentRecord) -> bool:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("temporary runtime failure")
        return await original_remove(agent)

    monkeypatch.setattr(service.agent_manager, "remove_container", remove)
    response = await scenario.client.post(
        "/api/tenants/transfer",
        headers=scenario.headers(scenario.applicant),
        json={
            "password": "qa-password",
            "invitation_code": "JOIN",
            "transfer_choice": "free",
            "free_destructive_consent": True,
        },
    )
    assert response.status_code == 202, response.text
    admission_id = PendingAdmissionResponse.model_validate_json(response.content).request.id
    response = await scenario.client.post(
        f"/api/company-admissions/{admission_id}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(scenario.genesis),
    )
    assert response.status_code == 200, response.text
    await background_tasks.drain_background_tasks(grace_seconds=5)
    row = await scenario.db.fetchone(
        "SELECT state,attempts,last_error FROM company_transfer_runtime_cleanup WHERE agent_id=%(id)s",
        {"id": agent_id},
    )
    assert row == {"state": "pending", "attempts": 1, "last_error": "RuntimeError"}
    user = await scenario.current_applicant()
    with pytest.raises(HTTPException) as pending:
        await check_agent_access(user, agent_id, fresh=True)
    assert pending.value.status_code == 423
    with pytest.raises(HTTPException) as gated:
        await assert_work_allowed(agent_id)
    assert gated.value.status_code == 423
    # When the worker retries, it cannot attest to the separate connector owner's shutdown.
    monkeypatch.setattr(get_settings(), "PROCESS_ROLE", "worker")
    await service.reconcile_transfer_cleanup()
    row = await scenario.db.fetchone(
        "SELECT state,attempts,last_error FROM company_transfer_runtime_cleanup WHERE agent_id=%(id)s",
        {"id": agent_id},
    )
    assert row == {"state": "pending", "attempts": 2, "last_error": "connector_cleanup_pending"}
    monkeypatch.setattr(get_settings(), "PROCESS_ROLE", "connector")
    await service.reconcile_transfer_cleanup()
    # Then durable completion replaces the temporary fence with former-owner denial.
    row = await scenario.db.fetchone(
        "SELECT state,attempts,last_error,completed_at IS NOT NULL AS completed "
        "FROM company_transfer_runtime_cleanup WHERE agent_id=%(id)s",
        {"id": agent_id},
    )
    assert row == {"state": "completed", "attempts": 3, "last_error": None, "completed": True}
    with pytest.raises(HTTPException) as denied:
        await check_agent_access(user, agent_id, fresh=True)
    assert denied.value.status_code == 403
    assert (workspace / "MEMORY.md").read_text() == "retained memory"
    assert (workspace / "report.txt").read_text() == "retained file"
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT * FROM messages").fetchall() == [("retained chat",)]
        assert db.execute("SELECT * FROM auth_profile_store").fetchall() == []
    assert not secret.exists()
    assert not (directory / "openclaw.json").exists()
    for query in (
        "SELECT count(*) FROM chat_messages WHERE agent_id=%(id)s",
        "SELECT count(*) FROM chat_sessions WHERE agent_id=%(id)s",
        "SELECT count(*) FROM tasks WHERE agent_id=%(id)s",
        "SELECT count(*) FROM audit_logs WHERE agent_id=%(id)s",
        "SELECT count(*) FROM approval_requests WHERE agent_id=%(id)s",
    ):
        assert await scenario.db.fetchval(query, {"id": agent_id}) == 1


async def test_pending_batch_rotates_past_first_hundred_failures(transfer_scenario: AdmissionScenario) -> None:
    # Given more pending agents than one bounded cleanup batch.
    scenario = transfer_scenario
    request_id = await submit_paid(scenario)
    for _ in range(101):
        agent = await agent_dao.create(
            obj_in={
                "id": uuid4(),
                "name": "Pending",
                "creator_id": scenario.applicant.id,
                "tenant_id": scenario.source_id,
            }
        )
        await scenario.db.execute(
            "INSERT INTO company_transfer_runtime_cleanup(agent_id,transfer_request_id,choice) "
            "VALUES(%(agent)s,%(request)s,'paid')",
            {"agent": agent.id, "request": request_id},
        )
    first = await cleanup.pending()
    first_ids: set[UUID] = {item.agent_id for item in first}
    assert len(first_ids) == 100
    # When the first batch all fails and remains pending.
    for item in first:
        await cleanup.record_attempt(item.agent_id, "workspace_busy")
    # Then the next bounded batch includes previously unattempted work.
    second_ids = {item.agent_id for item in await cleanup.pending()}
    assert len(second_ids) == 100
    assert len(second_ids - first_ids) == 1
