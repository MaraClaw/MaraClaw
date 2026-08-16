"""Tests for agent delete cleanup path (pure-psycopg / DAO)."""

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from python_on_whales.exceptions import DockerException

from app.api import agents as agents_api
from app.dao.agent_dao import AGENT_DELETE_CLEANUP_SQL

DELETE_AGENT_CLEANUP_SQL = AGENT_DELETE_CLEANUP_SQL


def make_user(**overrides):
    values = {
        "id": uuid.uuid4(),
        "username": "alice",
        "email": "alice@example.com",
        "password_hash": "hashed",
        "display_name": "Alice",
        "role": "member",
        "tenant_id": uuid.uuid4(),
        "is_active": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_agent(creator_id: uuid.UUID, **overrides):
    values = {
        "id": uuid.uuid4(),
        "name": "Ops Bot",
        "role_description": "assistant",
        "creator_id": creator_id,
        "status": "idle",
        "agent_type": "openclaw",
        "is_system": False,
        "tenant_id": uuid.uuid4(),
        "container_id": None,
        "container_port": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_delete_agent_cleans_remaining_foreign_key_rows(monkeypatch):
    creator = make_user()
    agent = make_agent(creator.id)
    executed: list[str] = []

    async def fake_check_agent_access(_current_user, _agent_id, _db=None):
        return agent, "manage"

    async def fake_delete_with_related(agent_id):
        assert agent_id == agent.id
        executed.extend(AGENT_DELETE_CLEANUP_SQL)
        executed.append("DELETE FROM agents WHERE id = %(aid)s")

    class FakeAgentManager:
        async def remove_container(self, _agent):
            return None

        async def archive_agent_files(self, _agent_id):
            return None

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(agents_api, "is_agent_creator", lambda _user, _agent: True)
    monkeypatch.setattr(agents_api.agent_dao, "delete_with_related", fake_delete_with_related)
    monkeypatch.setattr(agents_api, "agent_manager", FakeAgentManager())

    await agents_api.delete_agent(agent_id=agent.id, current_user=creator)

    task_logs_idx = executed.index(
        "DELETE FROM task_logs WHERE task_id IN (SELECT id FROM tasks WHERE agent_id = %(aid)s)"
    )
    tasks_idx = executed.index("DELETE FROM tasks WHERE agent_id = %(aid)s")
    assert task_logs_idx < tasks_idx
    assert executed[-1] == "DELETE FROM agents WHERE id = %(aid)s"


@pytest.mark.asyncio
async def test_delete_agent_uses_static_cleanup_statements_with_bound_agent_id(monkeypatch):
    creator = make_user()
    agent = make_agent(creator.id)
    called_with: list[uuid.UUID] = []

    async def fake_check_agent_access(_current_user, _agent_id, _db=None):
        return agent, "manage"

    async def fake_delete_with_related(agent_id):
        called_with.append(agent_id)

    class FakeAgentManager:
        async def remove_container(self, _agent):
            return None

        async def archive_agent_files(self, _agent_id):
            return None

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(agents_api, "is_agent_creator", lambda _user, _agent: True)
    monkeypatch.setattr(agents_api.agent_dao, "delete_with_related", fake_delete_with_related)
    monkeypatch.setattr(agents_api, "agent_manager", FakeAgentManager())

    await agents_api.delete_agent(agent_id=agent.id, current_user=creator)

    assert agents_api._DELETE_AGENT_CLEANUP_STATEMENTS == DELETE_AGENT_CLEANUP_SQL
    assert all("%(aid)s" in sql or "agent" in sql.lower() for sql in agents_api._DELETE_AGENT_CLEANUP_STATEMENTS)
    assert called_with == [agent.id]


@pytest.mark.asyncio
async def test_delete_agent_propagates_required_cleanup_failure_without_committing(monkeypatch):
    creator = make_user()
    agent = make_agent(creator.id)

    async def fake_check_agent_access(_current_user, _agent_id, _db=None):
        return agent, "manage"

    async def failing_delete(_agent_id):
        raise RuntimeError("required cleanup failed")

    class FakeAgentManager:
        async def remove_container(self, _agent):
            return None

        async def archive_agent_files(self, _agent_id):
            return None

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(agents_api, "is_agent_creator", lambda _user, _agent: True)
    monkeypatch.setattr(agents_api.agent_dao, "delete_with_related", failing_delete)
    monkeypatch.setattr(agents_api, "agent_manager", FakeAgentManager())

    with pytest.raises(RuntimeError, match="required cleanup failed"):
        await agents_api.delete_agent(agent_id=agent.id, current_user=creator)


@pytest.mark.asyncio
async def test_delete_agent_ignores_expected_container_and_archive_failures(monkeypatch):
    creator = make_user()
    agent = make_agent(creator.id)
    deleted: list[uuid.UUID] = []

    async def fake_check_agent_access(_current_user, _agent_id, _db=None):
        return agent, "manage"

    async def fake_delete_with_related(agent_id):
        deleted.append(agent_id)

    class FailingAgentManager:
        async def remove_container(self, _agent):
            raise DockerException(["docker", "rm"], 1, b"not found", b"container unavailable")

        async def archive_agent_files(self, _agent_id):
            raise OSError("archive unavailable")

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(agents_api, "is_agent_creator", lambda _user, _agent: True)
    monkeypatch.setattr(agents_api.agent_dao, "delete_with_related", fake_delete_with_related)
    monkeypatch.setattr(agents_api, "agent_manager", FailingAgentManager())

    await agents_api.delete_agent(agent_id=agent.id, current_user=creator)
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

    monkeypatch.setattr(agents_api.task_dao, "list_for_agent", list_for_agent)
    monkeypatch.setattr(agents_api.task_log_dao, "list_for_task", list_for_task)

    archive_dir = tmp_path / "_archived" / f"{agent_id}_20260325_120000"
    archive_path = await agents_api._archive_agent_task_history(agent_id, archive_dir)

    assert archive_path == archive_dir / "task_history.json"
    assert archive_path is not None
    payload = json.loads(archive_path.read_text(encoding="utf-8"))
    assert payload["agent_id"] == str(agent_id)
    assert payload["tasks"][0]["id"] == str(task_id)
    assert payload["tasks"][0]["logs"][0]["content"] == "Completed review and left comments"
