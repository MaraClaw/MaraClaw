import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import override

import pytest

from app.services import agent_tools, workspace_collaboration, workspace_locking
from app.services.agent_tool_exec import _agent_tool_exec_storage as storage, workspace, workspace_read
from app.services.agent_tool_exec._agent_tool_exec_storage import list_files as storage_list_files
from app.services.agent_tool_exec.registry import (
    ToolArguments,
    ToolArgumentValue,
    ToolExecutionContext,
    use_execution_context,
)
from app.services.focus_service import SerializedFocusItem
from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    StorageBackend,
    StorageEntry,
    StorageVersion,
    WriteCondition,
)


@pytest.fixture(autouse=True)
def _disable_redis_workspace_locks(monkeypatch):
    @asynccontextmanager
    async def _noop_workspace_locks(_agent_id, _paths, *, _ttl_seconds=60):
        yield

    monkeypatch.setattr(workspace_locking, "workspace_locks", _noop_workspace_locks)
    monkeypatch.setattr(workspace_collaboration, "workspace_locks", _noop_workspace_locks)
    from app.services.agent_tool_exec import workspace_temp

    monkeypatch.setattr(workspace_temp, "workspace_locks", _noop_workspace_locks)
    # agent_tools may re-export flush via workspace_temp
    if hasattr(agent_tools, "workspace_locks"):
        monkeypatch.setattr(agent_tools, "workspace_locks", _noop_workspace_locks, raising=False)


class MemoryStorageBackend(StorageBackend):
    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = dict(files or {})
        self.versions = dict.fromkeys(self.files, 1)

    @override
    async def exists(self, key: str) -> bool:
        return key in self.files

    @override
    async def is_file(self, key: str) -> bool:
        return key in self.files

    @override
    async def is_dir(self, key: str) -> bool:
        prefix = key.rstrip("/") + "/"
        return any(existing.startswith(prefix) for existing in self.files)

    @override
    async def list_dir(self, key: str) -> list[StorageEntry]:
        prefix = key.rstrip("/") + "/"
        entries: dict[str, StorageEntry] = {}
        for existing, data in self.files.items():
            if not existing.startswith(prefix):
                continue
            rest = existing.removeprefix(prefix)
            name, _, tail = rest.partition("/")
            entries[name] = StorageEntry(
                name=name,
                key=f"{prefix}{name}",
                is_dir=bool(tail),
                size=0 if tail else len(data),
            )
        return sorted(entries.values(), key=lambda entry: (not entry.is_dir, entry.name))

    @override
    async def read_bytes(self, key: str) -> bytes:
        return self.files[key]

    @override
    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.files[key] = data
        self.versions[key] = self.versions.get(key, 0) + 1

    @override
    async def delete(self, key: str) -> None:
        self.files.pop(key, None)
        self.versions.pop(key, None)

    @override
    async def delete_tree(self, key: str) -> None:
        prefix = key.rstrip("/") + "/"
        for existing in list(self.files):
            if existing.startswith(prefix):
                self.files.pop(existing)
                self.versions.pop(existing, None)

    @override
    async def stat(self, key: str) -> StorageEntry:
        return StorageEntry(name=key.rsplit("/", 1)[-1], key=key, is_dir=False, size=len(self.files[key]))

    @override
    async def get_version(self, key: str) -> StorageVersion:
        if key not in self.files:
            return StorageVersion(key=key, exists=False, is_dir=False)
        version = str(self.versions.get(key, 0))
        return StorageVersion(
            key=key,
            exists=True,
            is_dir=False,
            size=len(self.files[key]),
            version_id=version,
            etag=version,
            content_hash=version,
        )

    @override
    async def write_bytes_if_match(
        self,
        key: str,
        data: bytes,
        *,
        condition: WriteCondition | None = None,
        content_type: str | None = None,
    ) -> ConditionalWriteResult:
        current = await self.get_version(key)
        if condition:
            if condition.require_absent and current.exists:
                return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
            if condition.version_token is not None and current.token != condition.version_token:
                return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
        await self.write_bytes(key, data, content_type=content_type)
        return ConditionalWriteResult(ok=True, current_version=await self.get_version(key))


@pytest.fixture
async def unused_db_session() -> AsyncIterator[SimpleNamespace]:
    session = SimpleNamespace()
    try:
        yield session
    finally:
        pass


@pytest.mark.asyncio
async def test_get_agent_tenant_id_returns_database_tenant(monkeypatch):
    # Given
    from app.dao.agent_dao import agent_dao

    agent_id = uuid.uuid4()

    class _Agent:
        tenant_id = "tenant-a"

    async def _get(_agent_id):
        return _Agent()

    monkeypatch.setattr(agent_dao, "get", _get)

    # When
    tenant_id = await agent_tools._get_agent_tenant_id(agent_id)

    # Then
    assert tenant_id == "tenant-a"


@pytest.mark.asyncio
async def test_get_agent_tenant_id_returns_none_when_lookup_fails(monkeypatch):
    # Given
    from app.dao.agent_dao import agent_dao

    agent_id = uuid.uuid4()

    async def _get(_agent_id):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(agent_dao, "get", _get)

    # When
    tenant_id = await agent_tools._get_agent_tenant_id(agent_id)

    # Then
    assert tenant_id is None


def test_workspace_path_helpers_preserve_mapping_normalization_and_traversal(monkeypatch, tmp_path):
    # Given
    agent_id = uuid.uuid4()
    workspace = tmp_path / str(agent_id)
    report = workspace / "workspace" / "Report.md"
    report.parent.mkdir(parents=True)
    report.write_text("report", encoding="utf-8")
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    # When
    enterprise_root, enterprise_subpath = agent_tools._allowed_root_for_tool_path(
        workspace,
        "enterprise_info/catalog.md",
        tenant_id="tenant-a",
    )
    source = agent_tools._resolve_tool_source_path(workspace, "workspace/report .md")
    agent_key = agent_tools._tool_storage_key(agent_id, " .//workspace\\notes.md ")
    enterprise_key = agent_tools._tool_storage_key(agent_id, "enterprise_info/catalog.md", tenant_id="tenant-a")

    # Then
    assert agent_tools._agent_workspace_root(agent_id) == workspace
    assert agent_tools._normalize_tool_rel_path(" .//workspace\\notes.md ") == "workspace/notes.md"
    assert agent_tools._collapse_filename_for_match("Re\u0301 port .md") == "réport.md"
    assert enterprise_root == tmp_path / "enterprise_info_tenant-a"
    assert enterprise_subpath == "catalog.md"
    assert source == report
    assert agent_key == (f"{agent_id}/workspace/notes.md", "workspace/notes.md", False)
    assert enterprise_key == ("enterprise_info_tenant-a/catalog.md", "enterprise_info/catalog.md", True)
    with pytest.raises(ValueError, match="Access denied for this path"):
        agent_tools._resolve_tool_source_path(workspace, "workspace/../../escape.md")
    with pytest.raises(ValueError, match=re.escape("❌ Access denied.")):
        agent_tools._resolve_tool_target_path(workspace, "workspace/../../escape.md")


@pytest.mark.asyncio
async def test_storage_workspace_helpers_preserve_listing_read_search_and_find(monkeypatch):
    # Given
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend(
        {
            f"{agent_id}/workspace/notes.md": b"needle\n",
            f"{agent_id}/workspace/nested/child.txt": b"nested\n",
        }
    )
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    # When
    listing = await agent_tools._storage_list_dir(agent_id, "workspace")
    read = await agent_tools._storage_read_file(agent_id, "workspace/notes.md")
    search = await agent_tools._storage_search_files(agent_id, "needle", path="workspace", file_pattern="*.md")
    found = await agent_tools._storage_find_files(agent_id, "*.md", path="workspace")

    # Then
    assert listing == "📂 workspace: 1 folder(s), 1 file(s)\n  📁 nested/ (1 items)\n  📄 notes.md (7B)"
    assert read == "📄 workspace/notes.md (lines 1-1 of 1)\n     1\tneedle"
    assert search == "🔍 Found 1+ match(es) in 1 file(s) for pattern 'needle':\nworkspace/notes.md:1: needle"
    assert found == "📂 Found 1 item(s) (0 dirs, 1 files) matching '*.md':\n📄 workspace/notes.md (7B)"
    assert await agent_tools._storage_read_file(agent_id, "") == "File not found: root"
    assert await agent_tools._storage_search_files(agent_id, "needle", path="missing") == "Directory not found: missing"


@pytest.mark.asyncio
async def test_storage_handler_prefers_execution_context_tenant(monkeypatch, tmp_path):
    # Given
    agent_id = uuid.uuid4()

    async def _unexpected_tenant(_agent_id):
        return "fallback-tenant"

    async def _record_tenant(_agent_id, _path, tenant_id=None, **_dependencies):
        return str(tenant_id)

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", _unexpected_tenant)
    monkeypatch.setattr(workspace_read, "_storage_list_dir", _record_tenant)

    # When
    with use_execution_context(ToolExecutionContext(tenant_id="context-tenant", workspace_root=tmp_path)):
        handler_result = storage_list_files(
            arguments={"path": "enterprise_info"},
            agent_id=agent_id,
            user_id=agent_id,
            session_id="",
            on_output=None,
        )
        result = handler_result if isinstance(handler_result, str) else await handler_result

    # Then
    assert result == "context-tenant"


async def test_storage_read_callbacks_use_direct_owners_with_facade_dependencies(monkeypatch) -> None:
    agent_id = uuid.uuid4()
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def list_owner(received_agent_id: uuid.UUID, path: str, **kwargs: object) -> str:
        calls.append(("list", (received_agent_id, path), kwargs))
        return "list result"

    async def read_owner(received_agent_id: uuid.UUID, path: str, **kwargs: object) -> str:
        calls.append(("read", (received_agent_id, path), kwargs))
        return "read result"

    async def legacy_owner(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("storage callbacks must not call legacy facade wrappers")

    async def tenant_lookup(_: uuid.UUID) -> str:
        return "tenant-storage"

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(workspace_read, "_storage_list_dir", list_owner)
    monkeypatch.setattr(workspace_read, "_storage_read_file", read_owner)
    monkeypatch.setattr(agent_tools, "_storage_list_dir", legacy_owner)
    monkeypatch.setattr(agent_tools, "_storage_read_file", legacy_owner)

    list_result = storage.list_files(
        arguments={"path": "workspace"}, agent_id=agent_id, user_id=agent_id, session_id="storage", on_output=None
    )
    assert not isinstance(list_result, str)
    assert await list_result == "list result"
    read_result = storage.read_file(
        arguments={"path": "workspace/notes.md", "offset": 2, "limit": 3},
        agent_id=agent_id,
        user_id=agent_id,
        session_id="storage",
        on_output=None,
    )
    assert not isinstance(read_result, str)
    assert await read_result == "read result"

    assert calls == [
        (
            "list",
            (agent_id, "workspace"),
            {
                "tenant_id": "tenant-storage",
                "get_storage_backend": agent_tools.get_storage_backend,
                "tool_storage_key": agent_tools._tool_storage_key,
                "display_size": agent_tools._display_size,
            },
        ),
        (
            "read",
            (agent_id, "workspace/notes.md"),
            {
                "tenant_id": "tenant-storage",
                "offset": 2,
                "limit": 3,
                "get_storage_backend": agent_tools.get_storage_backend,
                "tool_storage_key": agent_tools._tool_storage_key,
            },
        ),
    ]


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("write_file", {"path": "workspace/new.md", "content": "new"}),
        ("move_file", {"source_path": "workspace/a.md", "destination_path": "workspace/b.md"}),
        ("delete_file", {"path": "workspace/a.md"}),
        ("edit_file", {"path": "workspace/a.md", "old_string": "old", "new_string": "new"}),
    ],
)
async def test_storage_mutation_callbacks_use_workspace_owner(
    monkeypatch, tmp_path, tool_name: str, arguments: ToolArguments
) -> None:
    agent_id = uuid.uuid4()
    calls: list[tuple[str, ToolArguments, uuid.UUID, Path, str | None]] = []

    async def mutation_owner(
        received_tool_name: str,
        received_arguments: ToolArguments,
        *,
        agent_id: uuid.UUID,
        base_dir: Path,
        session_id: str | None,
    ) -> str:
        calls.append((received_tool_name, received_arguments, agent_id, base_dir, session_id))
        return "mutation result"

    async def legacy_owner(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("storage callbacks must not call legacy mutation wrappers")

    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _: tmp_path)
    monkeypatch.setattr(workspace, "_execute_workspace_mutation", mutation_owner)
    monkeypatch.setattr(agent_tools, "_execute_workspace_mutation", legacy_owner)

    handler = getattr(storage, tool_name)
    handler_result = handler(
        arguments=arguments, agent_id=agent_id, user_id=agent_id, session_id="mutation", on_output=None
    )
    assert not isinstance(handler_result, str)
    assert await handler_result == "mutation result"
    assert calls == [(tool_name, arguments, agent_id, tmp_path, "mutation")]


async def test_storage_focus_callbacks_use_focus_service_owners(monkeypatch) -> None:
    agent_id = uuid.uuid4()
    calls: list[tuple[str, tuple[uuid.UUID, ...], ToolArguments]] = []
    focus_item: SerializedFocusItem = {
        "id": "focus-id",
        "agent_id": str(agent_id),
        "key": "task",
        "title": None,
        "description": "work",
        "status": "in_progress",
        "kind": "normal",
        "source": "user",
        "metadata": {},
        "sort_order": 0,
        "completed_at": None,
        "created_at": None,
        "updated_at": None,
    }

    async def list_owner(received_agent_id: uuid.UUID, *, include_completed: bool) -> list[SerializedFocusItem]:
        calls.append(("list", (received_agent_id,), {"include_completed": include_completed}))
        return [focus_item]

    async def upsert_owner(received_agent_id: uuid.UUID, **kwargs: ToolArgumentValue) -> SerializedFocusItem:
        calls.append(("upsert", (received_agent_id,), kwargs))
        return focus_item

    async def complete_owner(received_agent_id: uuid.UUID, *, key: str) -> SerializedFocusItem:
        calls.append(("complete", (received_agent_id,), {"key": key}))
        return focus_item

    monkeypatch.setattr(storage.focus_service, "list_focus_items", list_owner)
    monkeypatch.setattr(storage.focus_service, "upsert_focus_item", upsert_owner)
    monkeypatch.setattr(storage.focus_service, "complete_focus_item", complete_owner)

    list_result = storage.list_focus_items(
        arguments={"include_completed": False}, agent_id=agent_id, user_id=agent_id, session_id="focus", on_output=None
    )
    assert not isinstance(list_result, str)
    assert await list_result == "Focus items:\n- task [in_progress]: work"
    upsert_result = storage.upsert_focus_item(
        arguments={"description": "work"}, agent_id=agent_id, user_id=agent_id, session_id="focus", on_output=None
    )
    assert not isinstance(upsert_result, str)
    assert await upsert_result == "✅ Focus item saved: task - work"
    complete_result = storage.complete_focus_item(
        arguments={"key": "task"}, agent_id=agent_id, user_id=agent_id, session_id="focus", on_output=None
    )
    assert not isinstance(complete_result, str)
    assert await complete_result == "✅ Focus item completed: task"
    assert calls == [
        ("list", (agent_id,), {"include_completed": False}),
        (
            "upsert",
            (agent_id,),
            {
                "key": None,
                "title": None,
                "description": "work",
                "status": "in_progress",
                "kind": "normal",
                "source": "user",
                "metadata": {"tool": "upsert_focus_item"},
            },
        ),
        ("complete", (agent_id,), {"key": "task"}),
    ]


def test_local_workspace_read_helpers_preserve_listing_and_pagination(monkeypatch, tmp_path):
    # Given
    workspace = tmp_path / "agent"
    notes = workspace / "workspace" / "notes.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("first\nsecond\n", encoding="utf-8")
    (workspace / ".hidden").write_text("hidden", encoding="utf-8")
    (tmp_path / "enterprise_info_tenant-a").mkdir()
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    # When
    listing = agent_tools._list_files(workspace, "", tenant_id="tenant-a")
    read = agent_tools._read_file(workspace, "workspace/notes.md", offset=1, limit=1)
    empty = workspace / "empty.md"
    empty.write_text("", encoding="utf-8")
    empty_read = agent_tools._read_file(workspace, "empty.md")

    # Then
    assert "📁 enterprise_info/ (shared company info)" in listing
    assert ".hidden" not in listing
    assert read == "📄 workspace/notes.md (lines 2-2 of 2)\n     2\tsecond"
    assert empty_read == "Offset 0 exceeds file length (0 lines total)"


def test_local_workspace_search_and_find_preserve_filters_and_results(monkeypatch, tmp_path):
    # Given
    workspace = tmp_path / "agent"
    docs = workspace / "workspace"
    docs.mkdir(parents=True)
    (docs / "notes.md").write_text("Needle\n", encoding="utf-8")
    (docs / "ignored.png").write_bytes(b"binary")
    (docs / "large.txt").write_bytes(b"x" * 1025)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    # When
    search = agent_tools._search_files(workspace, "needle", path="workspace", file_pattern="*.md", ignore_case=True)
    found = agent_tools._find_files(workspace, "*.txt", path="workspace")
    missing = agent_tools._find_files(workspace, "*.csv", path="workspace")

    # Then
    assert search == "🔍 Found 1+ match(es) in 1 file(s) for pattern 'needle':\nworkspace/notes.md:1: Needle"
    assert found == "📂 Found 1 item(s) (0 dirs, 1 files) matching '*.txt':\n📄 workspace/large.txt (1KB)"
    assert missing == "No files matching pattern: *.csv"


def test_local_workspace_mutations_preserve_protections_errors_and_success(monkeypatch, tmp_path):
    # Given
    workspace = tmp_path / "agent"
    workspace.mkdir()
    target = workspace / "workspace" / "notes.md"
    target.parent.mkdir()
    target.write_text("one\none\n", encoding="utf-8")
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    # When
    protected_write = agent_tools._write_file(workspace, "tasks.json", "ignored")
    protected_enterprise = agent_tools._write_file(workspace, "enterprise_info/report.md", "ignored")
    write = agent_tools._write_file(workspace, "workspace/new.md", "hello")
    ambiguous_edit = agent_tools._edit_file(workspace, "workspace/notes.md", "one", "two")
    edit = agent_tools._edit_file(workspace, "workspace/notes.md", "one", "two", replace_all=True)
    protected_delete = agent_tools._delete_file(workspace, "soul.md")
    protected_tasks_delete = agent_tools._delete_file(workspace, "tasks.json")
    delete = agent_tools._delete_file(workspace, "workspace/new.md")

    # Then
    assert protected_write == "tasks.json is a legacy read-only snapshot. Use the task APIs/UI to manage tasks."
    assert (
        protected_enterprise
        == "enterprise_info is shared company context and is read-only for agents. Ask an admin to update it."
    )
    assert write == "✅ Written to workspace/new.md (5 chars)"
    assert (
        ambiguous_edit
        == "❌ 'old_string' appears 2 times in workspace/notes.md. Use replace_all=true or provide more context to make the match unique."
    )
    assert edit == "✅ Replaced 2 occurrence(s) in workspace/notes.md"
    assert target.read_text(encoding="utf-8") == "two\ntwo\n"
    assert protected_delete == "soul.md cannot be deleted (protected)"
    assert protected_tasks_delete == "tasks.json cannot be deleted (protected)"
    assert delete == "✅ Deleted workspace/new.md"


@pytest.mark.asyncio
async def test_workspace_mutation_rejects_tasks_snapshot_move_without_io(tmp_path):
    result = await workspace._execute_workspace_mutation(
        "move_file",
        {"source_path": "tasks.json", "destination_path": "workspace/tasks.json"},
        agent_id=uuid.uuid4(),
        base_dir=tmp_path,
        session_id=None,
    )

    # Then
    assert result == "❌ tasks.json cannot be moved (protected)"


def test_workspace_path_resolvers_reject_sibling_prefix_escape(monkeypatch, tmp_path):
    # Given
    workspace = tmp_path / "workspace"
    sibling_workspace = tmp_path / "workspace-escape"
    workspace.mkdir()
    sibling_workspace.mkdir()
    (sibling_workspace / "secret.md").write_text("secret", encoding="utf-8")
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    # When / Then
    with pytest.raises(ValueError, match="Access denied for this path"):
        agent_tools._resolve_tool_source_path(workspace, "nested/../../workspace-escape/secret.md")
    with pytest.raises(ValueError, match=re.escape("❌ Access denied.")):
        agent_tools._resolve_tool_target_path(workspace, "nested/../../workspace-escape/secret.md")


def test_local_workspace_helpers_reject_sibling_prefix_escape(monkeypatch, tmp_path):
    # Given
    workspace = tmp_path / "workspace"
    sibling_workspace = tmp_path / "workspace-escape"
    workspace.mkdir()
    sibling_workspace.mkdir()
    secret = sibling_workspace / "secret.md"
    secret.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    escape_path = "../workspace-escape"

    # When
    listing = agent_tools._list_files(workspace, escape_path)
    search = agent_tools._search_files(workspace, "secret", path=escape_path)
    found = agent_tools._find_files(workspace, "*.md", path=escape_path)
    write = agent_tools._write_file(workspace, f"{escape_path}/written.md", "escaped")
    delete = agent_tools._delete_file(workspace, f"{escape_path}/secret.md")
    edit = agent_tools._edit_file(workspace, f"{escape_path}/secret.md", "secret", "edited")

    # Then
    assert listing == "Access denied for this path"
    assert search == "Access denied for this path"
    assert found == "Access denied for this path"
    assert write == "Access denied for this path"
    assert delete == "Access denied for this path"
    assert edit == "Access denied for this path"
    assert secret.read_text(encoding="utf-8") == "secret"
    assert not (sibling_workspace / "written.md").exists()


def test_local_workspace_helpers_reject_enterprise_sibling_prefix_escape(monkeypatch, tmp_path):
    # Given
    workspace = tmp_path / "workspace"
    enterprise_root = tmp_path / "enterprise_info_tenant-a"
    sibling_enterprise_root = tmp_path / "enterprise_info_tenant-a-escape"
    workspace.mkdir()
    enterprise_root.mkdir()
    sibling_enterprise_root.mkdir()
    (sibling_enterprise_root / "secret.md").write_text("secret", encoding="utf-8")
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    escape_path = "enterprise_info/../enterprise_info_tenant-a-escape"

    # When
    listing = agent_tools._list_files(workspace, escape_path, tenant_id="tenant-a")
    search = agent_tools._search_files(workspace, "secret", path=escape_path, tenant_id="tenant-a")
    found = agent_tools._find_files(workspace, "*.md", path=escape_path, tenant_id="tenant-a")

    # Then
    assert listing == "Access denied for this path"
    assert search == "Access denied for this path"
    assert found == "Access denied for this path"


def test_workspace_helpers_allow_nested_paths_within_root(monkeypatch, tmp_path):
    # Given
    workspace = tmp_path / "workspace"
    nested_file = workspace / "nested" / "notes.md"
    nested_file.parent.mkdir(parents=True)
    nested_file.write_text("needle", encoding="utf-8")
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    # When
    source = agent_tools._resolve_tool_source_path(workspace, "nested/../nested/notes.md")
    target = agent_tools._resolve_tool_target_path(workspace, "nested/draft.md")
    listing = agent_tools._list_files(workspace, "nested")
    search = agent_tools._search_files(workspace, "needle", path="nested")

    # Then
    assert source == nested_file
    assert target == workspace / "nested" / "draft.md"
    assert listing == "📂 nested: 0 folder(s), 1 file(s)\n  📄 notes.md (6B)"
    assert search == "🔍 Found 1+ match(es) in 1 file(s) for pattern 'needle':\nnested/notes.md:1: needle"


@pytest.mark.asyncio
async def test_agent_file_tools_use_storage_paths(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend(
        {
            f"{agent_id}/workspace/notes.md": b"# Notes\nneedle\n",
            f"{agent_id}/memory/memory.md": b"# Memory\n",
        }
    )
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    listing = await agent_tools._storage_list_dir(agent_id, "workspace")
    read = await agent_tools._storage_read_file(agent_id, "workspace/notes.md")
    search = await agent_tools._storage_search_files(agent_id, "needle", path="workspace", file_pattern="*.md")
    found = await agent_tools._storage_find_files(agent_id, "*.md", path="workspace")

    assert "notes.md" in listing
    assert "needle" in read
    assert "workspace/notes.md:2" in search
    assert "workspace/notes.md" in found


@pytest.mark.asyncio
async def test_temp_workspace_materializes_only_requested_paths(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend(
        {
            f"{agent_id}/workspace/input.md": b"# Input\n",
            f"{agent_id}/workspace/other.md": b"# Other\n",
        }
    )
    from app.services.agent_tool_exec import workspace_temp

    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(workspace_temp, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(agent_id, paths=["workspace/input.md"])
    try:
        assert (temp_ws.root / "workspace" / "input.md").read_text(encoding="utf-8") == "# Input\n"
        assert not (temp_ws.root / "workspace" / "other.md").exists()
    finally:
        temp_ws.cleanup()


@pytest.mark.asyncio
async def test_execute_tool_list_files_does_not_create_persistent_workspace(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend(
        {
            f"{agent_id}/workspace/input.md": b"# Input\n",
        }
    )
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    async def _tenant(_agent_id):
        return None

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", _tenant)

    result = await agent_tools.execute_tool("list_files", {"path": "workspace"}, agent_id, agent_id)

    assert "input.md" in result
    assert not (tmp_path / str(agent_id)).exists()


@pytest.mark.asyncio
async def test_write_workspace_file_does_not_mirror_to_local_for_non_local_storage(
    monkeypatch, tmp_path, unused_db_session
):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend()
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    result = await workspace_collaboration.write_workspace_file(
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        path="workspace/test.md",
        content="hello",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
    )

    assert result.ok is True
    assert storage.files[f"{agent_id}/workspace/test.md"] == b"hello"
    assert not (tmp_path / str(agent_id) / "workspace" / "test.md").exists()


@pytest.mark.asyncio
async def test_flush_temp_workspace_only_writes_changed_files(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend(
        {
            f"{agent_id}/workspace/input.md": b"# Input\n",
            f"{agent_id}/workspace/other.md": b"# Other\n",
        }
    )
    from app.services.agent_tool_exec import workspace_temp

    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(workspace_temp, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(agent_id, paths=["workspace"])
    try:
        (temp_ws.root / "workspace" / "input.md").write_text("# Updated\n", encoding="utf-8")
        result = await agent_tools.flush_temp_workspace(temp_ws)
    finally:
        temp_ws.cleanup()

    assert result["updated"] == ["workspace/input.md"]
    assert "workspace/other.md" in result["skipped"]
    assert storage.files[f"{agent_id}/workspace/input.md"] == b"# Updated\n"
    assert storage.files[f"{agent_id}/workspace/other.md"] == b"# Other\n"


@pytest.mark.asyncio
async def test_flush_temp_workspace_fails_on_conflict(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend(
        {
            f"{agent_id}/workspace/input.md": b"# Input\n",
        }
    )
    from app.services.agent_tool_exec import workspace_temp

    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(workspace_temp, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(agent_id, paths=["workspace/input.md"])
    try:
        (temp_ws.root / "workspace" / "input.md").write_text("# Local change\n", encoding="utf-8")
        await storage.write_bytes(f"{agent_id}/workspace/input.md", b"# Remote change\n")
        result = await agent_tools.flush_temp_workspace(temp_ws)
    finally:
        temp_ws.cleanup()

    assert result["conflicted"] == ["workspace/input.md"]
    assert storage.files[f"{agent_id}/workspace/input.md"] == b"# Remote change\n"


@pytest.mark.asyncio
async def test_write_workspace_file_fails_on_expected_version_conflict(monkeypatch, tmp_path, unused_db_session):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend(
        {
            f"{agent_id}/workspace/test.md": b"old",
        }
    )
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    version = await storage.get_version(f"{agent_id}/workspace/test.md")
    await storage.write_bytes(f"{agent_id}/workspace/test.md", b"remote-new")
    result = await workspace_collaboration.write_workspace_file(
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        path="workspace/test.md",
        content="local-new",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
        expected_version_token=version.token,
    )

    assert result.ok is False
    assert "Conflict detected" in result.message
    assert storage.files[f"{agent_id}/workspace/test.md"] == b"remote-new"


@pytest.mark.asyncio
async def test_move_workspace_path_fails_when_source_changes(monkeypatch, tmp_path, unused_db_session):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend(
        {
            f"{agent_id}/workspace/source.md": b"old",
        }
    )
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    version = await storage.get_version(f"{agent_id}/workspace/source.md")
    await storage.write_bytes(f"{agent_id}/workspace/source.md", b"remote-new")
    result = await workspace_collaboration.move_workspace_path(
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        source_path="workspace/source.md",
        destination_path="workspace/dest.md",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
        expected_source_version_token=version.token,
    )

    assert result.ok is False
    assert "Conflict detected" in result.message
    assert f"{agent_id}/workspace/dest.md" not in storage.files


@pytest.mark.asyncio
async def test_delete_workspace_directory_uses_prefix_existence(monkeypatch, tmp_path, unused_db_session):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend(
        {
            f"{agent_id}/workspace/dir/a.txt": b"a",
            f"{agent_id}/workspace/dir/nested/b.txt": b"b",
        }
    )
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    result = await workspace_collaboration.delete_workspace_file(
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        path="workspace/dir",
        actor_type="user",
        actor_id=agent_id,
        enforce_human_lock=False,
    )

    assert result.ok is True
    assert f"{agent_id}/workspace/dir/a.txt" not in storage.files
    assert f"{agent_id}/workspace/dir/nested/b.txt" not in storage.files
