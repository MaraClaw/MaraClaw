"""Tests for agent delete cleanup path (pure-psycopg / DAO)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import agents as agents_api, background_tasks
from app.core.domain.agents import AgentStatus
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.dao.agent_dao import AGENT_DELETE_CLEANUP_SQL
from app.db import session
from app.records.agent import AgentRecord
from app.records.agent_deletion_cleanup import AgentDeletionCleanupRecord
from app.records.user import UserRecord
from app.services import agent_stops
from app.services.agent_deletion_archive import publish_deletion_archive
from deletion_content_pg import seed_deletion_content

if TYPE_CHECKING:
    from tenant_deletion_pg_fixtures import DeletionPostgres

pytest_plugins = ("tenant_deletion_pg_fixtures",)

DELETE_AGENT_CLEANUP_SQL = AGENT_DELETE_CLEANUP_SQL


def test_agent_delete_cleanup_uses_real_chat_message_and_gateway_sender_columns() -> None:
    """chat_messages has no sender_agent_id; that FK lives on gateway_messages."""
    assert not any("chat_messages SET sender_agent_id" in sql for sql in AGENT_DELETE_CLEANUP_SQL)
    assert any("DELETE FROM chat_messages WHERE agent_id = %(aid)s" in sql for sql in AGENT_DELETE_CLEANUP_SQL)
    assert any(
        "DELETE FROM gateway_messages WHERE sender_agent_id = %(aid)s" in sql for sql in AGENT_DELETE_CLEANUP_SQL
    )


def make_user(**overrides):
    return replace(
        UserRecord(
            id=uuid.uuid4(), display_name="Alice", role=UserRole.PLATFORM_ADMIN, tenant_id=uuid.uuid4(), is_active=True
        ),
        **overrides,
    )


def make_agent(creator_id: uuid.UUID, **overrides):
    return replace(
        AgentRecord(
            id=uuid.uuid4(),
            name="Ops Bot",
            role_description="assistant",
            creator_id=creator_id,
            status=AgentStatus.IDLE,
            agent_type="openclaw",
            is_system=False,
            tenant_id=uuid.uuid4(),
        ),
        **overrides,
    )


@pytest.fixture
def delete_runtime(monkeypatch):
    raw = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    @asynccontextmanager
    async def connection():
        yield raw

    monkeypatch.setattr(session, "get_pool", lambda: SimpleNamespace(connection=connection))
    monkeypatch.setattr(agents_api, "assert_agent_deletion_allowed", AsyncMock())

    async def snapshot(agents):
        return tuple(agent_stops.AgentTeardown(replace(agent), None) for agent in agents)

    monkeypatch.setattr(agents_api, "snapshot_agent_teardown", snapshot)
    monkeypatch.setattr(agent_stops, "complete_deletion_cleanup", AsyncMock())
    return raw


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["member", "agent_admin", "org_admin"])
async def test_direct_delete_is_denied_before_any_cleanup_for_non_platform_roles(monkeypatch, delete_runtime, role):
    creator = make_user(role=role)
    agent = make_agent(creator.id)
    delete = AsyncMock()
    snapshot = AsyncMock()
    monkeypatch.setattr(agents_api, "check_agent_access", AsyncMock(return_value=(agent, "manage")))
    monkeypatch.setattr(agents_api.agent_dao, "delete_with_related", delete)
    monkeypatch.setattr(agents_api, "snapshot_agent_teardown", snapshot)

    with pytest.raises(HTTPException) as denied:
        await agents_api.delete_agent(agent_id=agent.id, current_user=creator)

    assert denied.value.status_code == 403
    assert denied.value.detail == {
        "code": "agent_deletion_requires_request",
        "message": "Agents are deleted through an approval request: POST /agent-deletion-requests",
    }
    delete.assert_not_awaited()
    snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_agent_cleans_remaining_foreign_key_rows(monkeypatch, delete_runtime):
    creator = make_user()
    agent = make_agent(creator.id)
    executed: list[str] = []

    async def fake_check_agent_access(_current_user, _agent_id):
        return agent, "manage"

    async def fake_delete_with_related(agent_id):
        assert agent_id == agent.id
        executed.extend(AGENT_DELETE_CLEANUP_SQL)
        executed.append("DELETE FROM agents WHERE id = %(aid)s")

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(agents_api, "is_agent_creator", lambda _user, _agent: True)
    monkeypatch.setattr(agents_api.agent_dao, "delete_with_related", fake_delete_with_related)

    await agents_api.delete_agent(agent_id=agent.id, current_user=creator)
    await asyncio.gather(*tuple(background_tasks._tasks))

    task_logs_idx = executed.index(
        "DELETE FROM task_logs WHERE task_id IN (SELECT id FROM tasks WHERE agent_id = %(aid)s)"
    )
    tasks_idx = executed.index("DELETE FROM tasks WHERE agent_id = %(aid)s")
    assert task_logs_idx < tasks_idx
    assert executed[-1] == "DELETE FROM agents WHERE id = %(aid)s"


@pytest.mark.asyncio
async def test_delete_agent_uses_static_cleanup_statements_with_bound_agent_id(monkeypatch, delete_runtime):
    creator = make_user()
    agent = make_agent(creator.id)
    called_with: list[uuid.UUID] = []

    async def fake_check_agent_access(_current_user, _agent_id):
        return agent, "manage"

    async def fake_delete_with_related(agent_id):
        called_with.append(agent_id)

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(agents_api, "is_agent_creator", lambda _user, _agent: True)
    monkeypatch.setattr(agents_api.agent_dao, "delete_with_related", fake_delete_with_related)

    await agents_api.delete_agent(agent_id=agent.id, current_user=creator)
    await asyncio.gather(*tuple(background_tasks._tasks))

    assert agents_api._DELETE_AGENT_CLEANUP_STATEMENTS == DELETE_AGENT_CLEANUP_SQL
    assert all("%(aid)s" in sql or "agent" in sql.lower() for sql in agents_api._DELETE_AGENT_CLEANUP_STATEMENTS)
    assert called_with == [agent.id]


@pytest.mark.asyncio
async def test_delete_agent_propagates_required_cleanup_failure_without_committing(monkeypatch, delete_runtime):
    creator = make_user()
    agent = make_agent(creator.id)

    async def fake_check_agent_access(_current_user, _agent_id):
        return agent, "manage"

    async def failing_delete(_agent_id):
        raise RuntimeError("required cleanup failed")

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(agents_api, "is_agent_creator", lambda _user, _agent: True)
    monkeypatch.setattr(agents_api.agent_dao, "delete_with_related", failing_delete)
    cleanup = AsyncMock()
    monkeypatch.setattr(agent_stops, "complete_deletion_cleanup", cleanup)
    with pytest.raises(RuntimeError, match="required cleanup failed"):
        await agents_api.delete_agent(agent_id=agent.id, current_user=creator)
    delete_runtime.commit.assert_not_awaited()
    cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_agent_commit_survives_postcommit_cleanup_dispatch_failure(monkeypatch, delete_runtime):
    creator = make_user()
    agent = make_agent(creator.id)
    deleted: list[uuid.UUID] = []

    async def fake_check_agent_access(_current_user, _agent_id):
        return agent, "manage"

    async def fake_delete_with_related(agent_id):
        deleted.append(agent_id)

    async def failed_dispatch(agent_id):
        assert delete_runtime.commit.await_count == 1
        raise OSError("cleanup dispatch unavailable")

    dispatch = AsyncMock(side_effect=failed_dispatch)

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(agents_api, "is_agent_creator", lambda _user, _agent: True)
    monkeypatch.setattr(agents_api.agent_dao, "delete_with_related", fake_delete_with_related)
    monkeypatch.setattr(agent_stops, "complete_deletion_cleanup", dispatch)

    await agents_api.delete_agent(agent_id=agent.id, current_user=creator)
    with pytest.raises(OSError, match="cleanup dispatch unavailable"):
        await asyncio.gather(*tuple(background_tasks._tasks))
    assert deleted == [agent.id]
    dispatch.assert_awaited_once_with(agent.id)
    delete_runtime.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_archive_agent_task_history_writes_json_snapshot(tmp_path):
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    workspace = tmp_path / str(agent_id)
    workspace.mkdir()
    archive_dir = tmp_path / "_archived" / f"{agent_id}_deleted"
    history = json.dumps(
        {"agent_id": str(agent_id), "tasks": [{"id": str(task_id), "logs": [{"content": "Completed"}]}]}
    )
    job = AgentDeletionCleanupRecord(agent_id, "Ops Bot", uuid.uuid4(), None, None, workspace, archive_dir, history)
    publish_deletion_archive(job)
    archive_path = archive_dir / "task_history.json"

    payload = json.loads(archive_path.read_text(encoding="utf-8"))
    assert payload["agent_id"] == str(agent_id)
    assert payload["tasks"][0]["id"] == str(task_id)
    assert payload["tasks"][0]["logs"][0]["content"] == "Completed"


@pytest.mark.parametrize("rollback", [False, True])
async def test_delete_agent_waits_for_owning_request_commit(monkeypatch, delete_runtime, tmp_path, rollback):
    creator = make_user()
    agent = make_agent(creator.id)
    workspace = tmp_path / str(agent.id)
    workspace.mkdir()
    observed = []

    async def complete(agent_id):
        observed.append((agent_id, delete_runtime.commit.await_count, session.get_connection()))
        destination = tmp_path / "archive"
        workspace.rename(destination)

    monkeypatch.setattr(agents_api, "check_agent_access", AsyncMock(return_value=(agent, "manage")))
    monkeypatch.setattr(agents_api, "is_agent_creator", lambda user, record: True)
    monkeypatch.setattr(agents_api.agent_dao, "delete_with_related", AsyncMock())
    monkeypatch.setattr(agent_stops, "complete_deletion_cleanup", complete)
    owner = session.bind_crud_connection()
    assert isinstance(owner, AsyncGenerator)
    await anext(owner)
    await agents_api.delete_agent(agent.id, creator)
    assert workspace.exists()
    assert observed == []
    if rollback:
        with pytest.raises(RuntimeError, match="rollback"):
            await owner.athrow(RuntimeError("rollback"))
    else:
        with pytest.raises(StopAsyncIteration):
            await anext(owner)
    await asyncio.gather(*tuple(background_tasks._tasks))
    assert workspace.exists() is rollback
    assert observed == ([] if rollback else [(agent.id, 1, None)])


async def test_postgres_delete_agent_clears_plaza_children_and_peer_participant_messages(deletion_pg: DeletionPostgres):
    content = await seed_deletion_content(deletion_pg)
    app = FastAPI()
    app.include_router(agents_api.router, prefix="/api", dependencies=[Depends(session.bind_crud_connection)])
    app.dependency_overrides[get_current_user] = lambda: replace(deletion_pg.actor, role=UserRole.PLATFORM_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(f"/api/agents/{deletion_pg.agent_id}")
    assert response.status_code == 204
    assert response.content == b""
    await asyncio.gather(*tuple(background_tasks._tasks))
    assert await deletion_pg.rows("SELECT id FROM agents WHERE id = %s", (deletion_pg.agent_id,)) == []
    assert await deletion_pg.rows("SELECT id FROM plaza_posts WHERE id = %s", (content.agent_post_id,)) == []
    assert await deletion_pg.rows("SELECT id FROM participants WHERE ref_id = %s", (deletion_pg.agent_id,)) == []
    assert len(await deletion_pg.rows("SELECT id FROM chat_messages")) == 2
    assert len(await deletion_pg.rows("SELECT id FROM chat_sessions")) == 2
    assert await deletion_pg.rows(
        "SELECT likes_count, comments_count FROM plaza_posts WHERE id = %s",
        (content.surviving_post_id,),
    ) == [{"likes_count": 2, "comments_count": 2}]
    assert len(await deletion_pg.rows("SELECT id FROM web_search_events")) == 1
    assert deletion_pg.runtime_calls == [("remove", deletion_pg.agent_id), ("archive", deletion_pg.agent_id)]
    archive = deletion_pg.runtime_root / "archive" / str(deletion_pg.agent_id)
    history = json.loads((archive / "task_history.json").read_text())
    assert history["tasks"][0]["title"] == "History survives"
    assert history["tasks"][0]["logs"][0]["content"] == "Completed"
    assert (archive / "SOUL.md").exists()


async def test_postgres_agent_delete_rollback_preserves_database_container_and_workspace(deletion_pg: DeletionPostgres):
    await seed_deletion_content(deletion_pg)
    before = await deletion_pg.snapshot()

    async def fail_request():
        async with session.connection_ctx():
            await agents_api.delete_agent(
                deletion_pg.agent_id, replace(deletion_pg.actor, role=UserRole.PLATFORM_ADMIN)
            )
            assert deletion_pg.runtime_calls == []
            raise RuntimeError("later request failure")

    with pytest.raises(RuntimeError, match="later request failure"):
        await fail_request()
    await asyncio.gather(*tuple(background_tasks._tasks))
    assert await deletion_pg.snapshot() == before
    assert deletion_pg.runtime_calls == []
    assert (deletion_pg.runtime_root / str(deletion_pg.agent_id) / "SOUL.md").exists()
    assert not (deletion_pg.runtime_root / "archive").exists()
