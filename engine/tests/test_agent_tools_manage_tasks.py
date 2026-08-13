from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import agent_tools
from app.services.agent_tools import ToolParameters


class FakeLogger:
    def __init__(self):
        self.errors = []

    def error(self, message):
        self.errors.append(message)


@pytest.fixture
def tasks_tool():
    return importlib.import_module("app.services.agent_tool_exec.tasks_tool")


@pytest.mark.asyncio
async def test_facade_task_helpers_defer_to_tasks_module(monkeypatch, tmp_path: Path):
    # Given
    tasks_tool = importlib.import_module("app.services.agent_tool_exec.tasks_tool")
    sync_tasks_to_file = AsyncMock()
    manage_tasks = AsyncMock(return_value="managed")
    monkeypatch.setattr(tasks_tool, "_sync_tasks_to_file", sync_tasks_to_file)
    monkeypatch.setattr(tasks_tool, "_manage_tasks", manage_tasks)
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    arguments: ToolParameters = {"action": "create", "title": "Ship"}

    # When
    await agent_tools._sync_tasks_to_file(agent_id, tmp_path)
    result = await agent_tools._manage_tasks(agent_id, user_id, tmp_path, arguments)

    # Then
    assert result == "managed"
    sync_tasks_to_file.assert_awaited_once_with(agent_id, tmp_path)
    manage_tasks.assert_awaited_once_with(agent_id, user_id, tmp_path, arguments)


@pytest.mark.asyncio
async def test_sync_tasks_to_file_skips_missing_snapshot_without_opening_database(monkeypatch, tasks_tool, tmp_path):
    # Given
    list_for_agent = AsyncMock(side_effect=AssertionError("missing snapshot must not open the database"))
    monkeypatch.setattr(tasks_tool.task_dao, "list_for_agent", list_for_agent)

    # When
    await tasks_tool._sync_tasks_to_file(uuid.uuid4(), tmp_path)

    # Then
    assert not (tmp_path / "tasks.json").exists()
    list_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_tasks_to_file_serializes_unicode_nulls_and_newest_first_query(monkeypatch, tasks_tool, tmp_path):
    # Given
    tasks_path = tmp_path / "tasks.json"
    tasks_path.touch()
    agent_id = uuid.uuid4()
    newest = SimpleNamespace(
        title="跟进客户",
        status="pending",
        priority="high",
        description=None,
        created_at=datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC),
        completed_at=None,
    )
    oldest = SimpleNamespace(
        title="旧任务",
        status="done",
        priority="low",
        description="已完成",
        created_at=None,
        completed_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
    list_for_agent = AsyncMock(return_value=[newest, oldest])
    monkeypatch.setattr(tasks_tool.task_dao, "list_for_agent", list_for_agent)

    # When
    await tasks_tool._sync_tasks_to_file(agent_id, tmp_path)

    # Then
    assert tasks_path.read_text(encoding="utf-8") == (
        "[\n"
        "  {\n"
        '    "title": "跟进客户",\n'
        '    "status": "pending",\n'
        '    "priority": "high",\n'
        '    "description": "",\n'
        '    "created_at": "2026-02-03T04:05:06+00:00",\n'
        '    "completed_at": ""\n'
        "  },\n"
        "  {\n"
        '    "title": "旧任务",\n'
        '    "status": "done",\n'
        '    "priority": "low",\n'
        '    "description": "已完成",\n'
        '    "created_at": "",\n'
        '    "completed_at": "2026-01-02T03:04:05+00:00"\n'
        "  }\n"
        "]"
    )
    list_for_agent.assert_awaited_once_with(agent_id)


@pytest.mark.asyncio
async def test_sync_tasks_to_file_logs_and_swallows_database_failure(monkeypatch, tasks_tool, tmp_path):
    # Given
    (tmp_path / "tasks.json").touch()
    monkeypatch.setattr(tasks_tool.task_dao, "list_for_agent", AsyncMock(side_effect=RuntimeError("database failed")))
    logger = FakeLogger()
    monkeypatch.setattr(tasks_tool, "logger", logger, raising=False)

    # When
    await tasks_tool._sync_tasks_to_file(uuid.uuid4(), tmp_path)

    # Then
    assert logger.errors == ["[AgentTools] Failed to sync tasks: database failed"]


@pytest.mark.asyncio
async def test_sync_tasks_to_file_logs_and_swallows_write_failure(monkeypatch, tasks_tool, tmp_path):
    # Given
    (tmp_path / "tasks.json").touch()
    monkeypatch.setattr(tasks_tool.task_dao, "list_for_agent", AsyncMock(return_value=[]))
    logger = FakeLogger()
    monkeypatch.setattr(tasks_tool, "logger", logger, raising=False)

    def failing_write(_path, _text, *, encoding):
        raise OSError("write failed")

    monkeypatch.setattr(Path, "write_text", failing_write)

    # When
    await tasks_tool._sync_tasks_to_file(uuid.uuid4(), tmp_path)

    # Then
    assert logger.errors == ["[AgentTools] Failed to sync tasks: write failed"]


@pytest.mark.asyncio
async def test_manage_tasks_creates_todo_with_defaults_schedules_and_syncs(monkeypatch, tasks_tool, tmp_path):
    # Given
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    task_id = uuid.uuid4()
    created = SimpleNamespace(id=task_id, title="Ship")
    create = AsyncMock(return_value=created)
    sync_tasks_to_file = AsyncMock()
    execute_task = AsyncMock()
    scheduled = []

    def schedule(coroutine):
        scheduled.append(coroutine)
        coroutine.close()
        return object()

    monkeypatch.setattr(tasks_tool.task_dao, "create", create)
    monkeypatch.setattr(tasks_tool, "_sync_tasks_to_file", sync_tasks_to_file)
    monkeypatch.setattr("app.services.task_executor.execute_task", execute_task)
    monkeypatch.setattr("asyncio.create_task", schedule)

    # When
    result = await tasks_tool._manage_tasks(agent_id, user_id, tmp_path, {"action": "create", "title": "Ship"})

    # Then
    assert result == "✅ Task created: Ship - auto-execution started"
    create.assert_awaited_once()
    obj_in = create.await_args.kwargs["obj_in"]
    assert obj_in == {
        "agent_id": agent_id,
        "title": "Ship",
        "description": None,
        "type": "todo",
        "priority": "medium",
        "created_by": user_id,
        "status": "pending",
        "supervision_target_name": None,
        "supervision_channel": "feishu",
        "remind_schedule": None,
    }
    execute_task.assert_called_once_with(task_id, agent_id)
    assert len(scheduled) == 1
    sync_tasks_to_file.assert_awaited_once_with(agent_id, tmp_path)


@pytest.mark.asyncio
async def test_manage_tasks_creates_supervision_with_defaults_without_scheduling(monkeypatch, tasks_tool, tmp_path):
    # Given
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    created = SimpleNamespace(id=uuid.uuid4(), title="Check in")
    create = AsyncMock(return_value=created)
    sync_tasks_to_file = AsyncMock()

    def unexpected_schedule(_coroutine):
        raise AssertionError("supervision task must not schedule execution")

    monkeypatch.setattr(tasks_tool.task_dao, "create", create)
    monkeypatch.setattr(tasks_tool, "_sync_tasks_to_file", sync_tasks_to_file)
    monkeypatch.setattr("asyncio.create_task", unexpected_schedule)

    # When
    result = await tasks_tool._manage_tasks(
        agent_id,
        user_id,
        tmp_path,
        {"action": "create", "title": "Check in", "task_type": "supervision"},
    )

    # Then
    assert result == "✅ Supervision task created: 'Check in' - will remind someone on schedule (not set)"
    obj_in = create.await_args.kwargs["obj_in"]
    assert obj_in["supervision_channel"] == "feishu"
    assert obj_in["type"] == "supervision"
    sync_tasks_to_file.assert_awaited_once_with(agent_id, tmp_path)


@pytest.mark.asyncio
async def test_manage_tasks_marks_done_with_utc_completion_and_syncs(monkeypatch, tasks_tool, tmp_path):
    # Given
    agent_id = uuid.uuid4()
    task = SimpleNamespace(id=uuid.uuid4(), title="Ship", status="pending", completed_at=None)
    find = AsyncMock(return_value=task)
    update = AsyncMock(return_value=task)
    sync_tasks_to_file = AsyncMock()
    monkeypatch.setattr(tasks_tool.task_dao, "find_first_by_title_ilike", find)
    monkeypatch.setattr(tasks_tool.task_dao, "update", update)
    monkeypatch.setattr(tasks_tool, "_sync_tasks_to_file", sync_tasks_to_file)

    # When
    result = await tasks_tool._manage_tasks(
        agent_id,
        uuid.uuid4(),
        tmp_path,
        {"action": "update_status", "title": "hip", "status": "done"},
    )

    # Then
    assert result == "✅ Updated 'Ship' from pending to done"
    find.assert_awaited_once_with(agent_id, "hip")
    update.assert_awaited_once()
    assert update.await_args.kwargs["obj_in"]["status"] == "done"
    assert update.await_args.kwargs["obj_in"]["completed_at"].tzinfo is UTC
    sync_tasks_to_file.assert_awaited_once_with(agent_id, tmp_path)


@pytest.mark.asyncio
async def test_manage_tasks_updates_non_done_without_changing_completion(monkeypatch, tasks_tool, tmp_path):
    # Given
    task = SimpleNamespace(id=uuid.uuid4(), title="Ship", status="pending", completed_at=object())
    update = AsyncMock(return_value=task)
    monkeypatch.setattr(tasks_tool.task_dao, "find_first_by_title_ilike", AsyncMock(return_value=task))
    monkeypatch.setattr(tasks_tool.task_dao, "update", update)
    monkeypatch.setattr(tasks_tool, "_sync_tasks_to_file", AsyncMock())

    # When
    result = await tasks_tool._manage_tasks(
        uuid.uuid4(),
        uuid.uuid4(),
        tmp_path,
        {"action": "update_status", "title": "Ship", "status": "doing"},
    )

    # Then
    assert result == "✅ Updated 'Ship' from pending to doing"
    assert update.await_args.kwargs["obj_in"] == {"status": "doing"}


@pytest.mark.asyncio
async def test_manage_tasks_returns_not_found_without_sync_for_status_update(monkeypatch, tasks_tool, tmp_path):
    # Given
    sync_tasks_to_file = AsyncMock()
    monkeypatch.setattr(tasks_tool.task_dao, "find_first_by_title_ilike", AsyncMock(return_value=None))
    monkeypatch.setattr(tasks_tool, "_sync_tasks_to_file", sync_tasks_to_file)

    # When
    result = await tasks_tool._manage_tasks(
        uuid.uuid4(),
        uuid.uuid4(),
        tmp_path,
        {"action": "update_status", "title": "missing", "status": "done"},
    )

    # Then
    assert result == "No task found matching 'missing'"
    sync_tasks_to_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_manage_tasks_deletes_logs_before_task_then_syncs(monkeypatch, tasks_tool, tmp_path):
    # Given
    agent_id = uuid.uuid4()
    task = SimpleNamespace(id=uuid.uuid4(), title="Ship")
    delete_with_logs = AsyncMock(return_value=task)
    sync_tasks_to_file = AsyncMock()
    monkeypatch.setattr(tasks_tool.task_dao, "find_first_by_title_ilike", AsyncMock(return_value=task))
    monkeypatch.setattr(tasks_tool.task_dao, "delete_with_logs", delete_with_logs)
    monkeypatch.setattr(tasks_tool, "_sync_tasks_to_file", sync_tasks_to_file)

    # When
    result = await tasks_tool._manage_tasks(
        agent_id,
        uuid.uuid4(),
        tmp_path,
        {"action": "delete", "title": "hip"},
    )

    # Then
    assert result == "✅ Task deleted: Ship"
    delete_with_logs.assert_awaited_once_with(task.id)
    sync_tasks_to_file.assert_awaited_once_with(agent_id, tmp_path)


@pytest.mark.asyncio
async def test_manage_tasks_returns_not_found_without_deleting_or_syncing(monkeypatch, tasks_tool, tmp_path):
    # Given
    delete_with_logs = AsyncMock()
    sync_tasks_to_file = AsyncMock()
    monkeypatch.setattr(tasks_tool.task_dao, "find_first_by_title_ilike", AsyncMock(return_value=None))
    monkeypatch.setattr(tasks_tool.task_dao, "delete_with_logs", delete_with_logs)
    monkeypatch.setattr(tasks_tool, "_sync_tasks_to_file", sync_tasks_to_file)

    # When
    result = await tasks_tool._manage_tasks(
        uuid.uuid4(),
        uuid.uuid4(),
        tmp_path,
        {"action": "delete", "title": "missing"},
    )

    # Then
    assert result == "No task found matching 'missing'"
    delete_with_logs.assert_not_awaited()
    sync_tasks_to_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_manage_tasks_returns_unknown_action_without_dao(monkeypatch, tasks_tool, tmp_path):
    # Given
    create = AsyncMock()
    monkeypatch.setattr(tasks_tool.task_dao, "create", create)

    # When
    result = await tasks_tool._manage_tasks(
        uuid.uuid4(),
        uuid.uuid4(),
        tmp_path,
        {"action": "archive", "title": "Ship"},
    )

    # Then
    assert result == "Unknown action: archive"
    create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{}, {"action": "create"}])
async def test_manage_tasks_raises_key_error_for_missing_required_keys(monkeypatch, tasks_tool, tmp_path, arguments):
    # When / Then
    with pytest.raises(KeyError):
        await tasks_tool._manage_tasks(uuid.uuid4(), uuid.uuid4(), tmp_path, arguments)
