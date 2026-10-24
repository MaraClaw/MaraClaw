"""Tests for agent delete cleanup path (pure-psycopg / DAO)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from python_on_whales.exceptions import DockerException

from app.api import agents as agents_api, background_tasks
from app.core.security import get_current_user
from app.dao.agent_dao import AGENT_DELETE_CLEANUP_SQL
from app.db import session
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.services import agent_deletion_history, agent_stops
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
        UserRecord(id=uuid.uuid4(), display_name="Alice", role="member", tenant_id=uuid.uuid4(), is_active=True),
        **overrides,
    )


def make_agent(creator_id: uuid.UUID, **overrides):
    return replace(
        AgentRecord(
            id=uuid.uuid4(),
            name="Ops Bot",
            role_description="assistant",
            creator_id=creator_id,
            status="idle",
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
    monkeypatch.setattr(agent_deletion_history.task_dao, "list_for_agent", AsyncMock(return_value=[]))
    return raw


@pytest.mark.asyncio
async def test_delete_agent_cleans_remaining_foreign_key_rows(monkeypatch, delete_runtime, tmp_path: Path):
    creator = make_user()
    agent = make_agent(creator.id)
    executed: list[str] = []

    async def fake_check_agent_access(_current_user, _agent_id):
        return agent, "manage"

    async def fake_delete_with_related(agent_id):
        assert agent_id == agent.id
        executed.extend(AGENT_DELETE_CLEANUP_SQL)
        executed.append("DELETE FROM agents WHERE id = %(aid)s")

    class FakeAgentManager:
        def _agent_dir(self, agent_id: uuid.UUID) -> Path:
            return tmp_path / str(agent_id)

        async def remove_container(self, _agent):
            return None

        async def archive_agent_files(self, _agent_id):
            return None

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(agents_api, "is_agent_creator", lambda _user, _agent: True)
    monkeypatch.setattr(agents_api.agent_dao, "delete_with_related", fake_delete_with_related)
    monkeypatch.setattr(agent_stops, "agent_manager", FakeAgentManager())

    await agents_api.delete_agent(agent_id=agent.id, current_user=creator)
    await asyncio.gather(*tuple(background_tasks._tasks))

    task_logs_idx = executed.index(
        "DELETE FROM task_logs WHERE task_id IN (SELECT id FROM tasks WHERE agent_id = %(aid)s)"
    )
    tasks_idx = executed.index("DELETE FROM tasks WHERE agent_id = %(aid)s")
    assert task_logs_idx < tasks_idx
    assert executed[-1] == "DELETE FROM agents WHERE id = %(aid)s"


@pytest.mark.asyncio
async def test_delete_agent_uses_static_cleanup_statements_with_bound_agent_id(
    monkeypatch, delete_runtime, tmp_path: Path
):
    creator = make_user()
    agent = make_agent(creator.id)
    called_with: list[uuid.UUID] = []

    async def fake_check_agent_access(_current_user, _agent_id):
        return agent, "manage"

    async def fake_delete_with_related(agent_id):
        called_with.append(agent_id)

    class FakeAgentManager:
        def _agent_dir(self, agent_id: uuid.UUID) -> Path:
            return tmp_path / str(agent_id)

        async def remove_container(self, _agent):
            return None

        async def archive_agent_files(self, _agent_id):
            return None

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(agents_api, "is_agent_creator", lambda _user, _agent: True)
    monkeypatch.setattr(agents_api.agent_dao, "delete_with_related", fake_delete_with_related)
    monkeypatch.setattr(agent_stops, "agent_manager", FakeAgentManager())

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
    manager = SimpleNamespace(remove_container=AsyncMock(), archive_agent_files=AsyncMock())
    monkeypatch.setattr(agent_stops, "agent_manager", manager)

    with pytest.raises(RuntimeError, match="required cleanup failed"):
        await agents_api.delete_agent(agent_id=agent.id, current_user=creator)
    delete_runtime.commit.assert_not_awaited()
    manager.remove_container.assert_not_awaited()
    manager.archive_agent_files.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_agent_ignores_expected_container_and_archive_failures(
    monkeypatch, delete_runtime, tmp_path: Path
):
    creator = make_user()
    agent = make_agent(creator.id)
    deleted: list[uuid.UUID] = []

    async def fake_check_agent_access(_current_user, _agent_id):
        return agent, "manage"

    async def fake_delete_with_related(agent_id):
        deleted.append(agent_id)

    class FailingAgentManager:
        def _agent_dir(self, agent_id: uuid.UUID) -> Path:
            return tmp_path / str(agent_id)

        async def remove_container(self, _agent):
            raise DockerException(["docker", "rm"], 1, b"not found", b"container unavailable")

        async def archive_agent_files(self, _agent_id):
            raise OSError("archive unavailable")

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(agents_api, "is_agent_creator", lambda _user, _agent: True)
    monkeypatch.setattr(agents_api.agent_dao, "delete_with_related", fake_delete_with_related)
    monkeypatch.setattr(agent_stops, "agent_manager", FailingAgentManager())

    await agents_api.delete_agent(agent_id=agent.id, current_user=creator)
    await asyncio.gather(*tuple(background_tasks._tasks))
    assert deleted == [agent.id]


@pytest.mark.asyncio
async def test_archive_agent_task_history_writes_json_snapshot(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    created_at = datetime.now(UTC)

    task = SimpleNamespace(
        id=task_id,
        title="Review PR",
        description="Check lore trailers",
        type="todo",
        status="done",
        priority="high",
        assignee="self",
        created_by=uuid.uuid4(),
        due_date=None,
        supervision_target_user_id=None,
        supervision_target_name=None,
        supervision_channel=None,
        remind_schedule=None,
        created_at=created_at,
        updated_at=created_at,
        completed_at=created_at,
    )
    log = SimpleNamespace(
        id=uuid.uuid4(),
        content="Completed review and left comments",
        created_at=created_at,
    )

    async def list_for_agent(_agent_id, *, ascending=False):
        assert ascending is True
        return [task]

    async def list_for_task(_task_id):
        return [log]

    monkeypatch.setattr(agent_deletion_history.task_dao, "list_for_agent", list_for_agent)
    monkeypatch.setattr(agent_deletion_history.task_log_dao, "list_for_task", list_for_task)

    archive_dir = tmp_path / "_archived" / f"{agent_id}_20260325_120000"
    archive_dir.mkdir(parents=True)
    monkeypatch.setattr(
        agent_stops,
        "agent_manager",
        SimpleNamespace(
            remove_container=AsyncMock(),
            archive_agent_files=AsyncMock(return_value=archive_dir),
            _agent_dir=lambda agent_id: tmp_path / str(agent_id),
        ),
    )
    snapshots = await agent_stops.snapshot_agent_teardown([make_agent(uuid.uuid4(), id=agent_id)])
    assert not (archive_dir / "task_history.json").exists()
    monkeypatch.setattr(agent_deletion_history.task_dao, "list_for_agent", AsyncMock(return_value=[]))
    await agent_stops.teardown_deleted_agents(snapshots)
    archive_path = archive_dir / "task_history.json"

    assert archive_path == archive_dir / "task_history.json"
    assert archive_path is not None
    payload = json.loads(archive_path.read_text(encoding="utf-8"))
    assert payload["agent_id"] == str(agent_id)
    assert payload["tasks"][0]["id"] == str(task_id)
    assert payload["tasks"][0]["logs"][0]["content"] == "Completed review and left comments"


@pytest.mark.parametrize("rollback", [False, True])
async def test_delete_agent_waits_for_owning_request_commit(monkeypatch, delete_runtime, tmp_path, rollback):
    creator = make_user()
    agent = make_agent(creator.id)
    workspace = tmp_path / str(agent.id)
    workspace.mkdir()
    observed = []

    async def remove(record):
        observed.append((record.id, delete_runtime.commit.await_count, session.get_connection()))

    async def archive(agent_id):
        destination = tmp_path / "archive"
        workspace.rename(destination)
        return destination

    monkeypatch.setattr(agents_api, "check_agent_access", AsyncMock(return_value=(agent, "manage")))
    monkeypatch.setattr(agents_api, "is_agent_creator", lambda user, record: True)
    monkeypatch.setattr(agents_api.agent_dao, "delete_with_related", AsyncMock())
    monkeypatch.setattr(
        agent_stops,
        "agent_manager",
        SimpleNamespace(
            remove_container=remove,
            archive_agent_files=archive,
            _agent_dir=lambda agent_id: tmp_path / str(agent_id),
        ),
    )
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
    app.dependency_overrides[get_current_user] = lambda: deletion_pg.actor
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
            await agents_api.delete_agent(deletion_pg.agent_id, deletion_pg.actor)
            assert deletion_pg.runtime_calls == []
            raise RuntimeError("later request failure")

    with pytest.raises(RuntimeError, match="later request failure"):
        await fail_request()
    await asyncio.gather(*tuple(background_tasks._tasks))
    assert await deletion_pg.snapshot() == before
    assert deletion_pg.runtime_calls == []
    assert (deletion_pg.runtime_root / str(deletion_pg.agent_id) / "SOUL.md").exists()
    assert not (deletion_pg.runtime_root / "archive").exists()
