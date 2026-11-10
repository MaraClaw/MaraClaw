import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import override

import pytest
from pydantic import BaseModel

from app.core.domain.focus import FocusKind, FocusStatus
from app.services import agent_tools, workspace_collaboration, workspace_locking
from app.services.agent_tool_exec import _agent_tool_exec_storage as storage, workspace, workspace_read
from app.services.agent_tool_exec._agent_tool_exec_storage import list_files as storage_list_files
from app.services.agent_tool_exec.registry import (
    ToolArguments,
    ToolArgumentValue,
    ToolExecutionContext,
    resolve,
    use_execution_context,
)
from app.services.focus_service import SerializedFocusItem
from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    StorageBackend,
    StorageEntry,
    StoragePage,
    StorageReadLimitExceededError,
    StorageSnapshot,
    StorageVersion,
    WriteCondition,
    content_hash_bytes,
)
from app.services.storage_runtime.fallback import FallbackStorageBackend
from app.services.storage_runtime.local import LocalStorageBackend


@pytest.mark.parametrize("source", ["local", "storage", "enterprise"])
async def test_read_document_cancel_reaches_owned_parser(monkeypatch, tmp_path, source):
    # Given
    from app.services.agent_tool_exec import document_reading, documents

    agent_id = uuid.uuid4()
    tenant_id = str(uuid.uuid4())
    rel = "enterprise_info/report.docx" if source == "enterprise" else "workspace/report.docx"
    key = f"enterprise_info_{tenant_id}/report.docx" if source == "enterprise" else f"{agent_id}/{rel}"
    backend = MemoryStorageBackend({key: b"document"})
    target = tmp_path / "workspace/report.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"document")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def parse(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(document_reading, "convert_document_isolated_async", parse, raising=False)
    monkeypatch.setattr("app.services.storage.get_storage_backend", lambda: backend)
    operation = (
        documents._read_document(tmp_path, rel)
        if source == "local"
        else documents._read_document_from_storage(agent_id, rel, tenant_id=tenant_id)
    )
    task = asyncio.create_task(operation)
    # When
    try:
        await asyncio.wait_for(started.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    # Then
    assert cancelled.is_set()


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
        self.files: dict[str, bytes] = dict(files or {})
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
    async def read_bytes_bounded(self, key: str, max_bytes: int, *, offset: int | None = None) -> bytes:
        if key not in self.files:
            raise FileNotFoundError(key)
        if offset is not None:
            return self.files[key][offset : offset + max_bytes]
        data = self.files[key][: max_bytes + 1]
        if len(data) > max_bytes:
            raise StorageReadLimitExceededError(max_bytes, len(data))
        return data

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
    async def get_version_bounded(self, key: str, max_bytes: int) -> StorageVersion:
        if key in self.files:
            await self.read_bytes_bounded(key, max_bytes)
        return await self.get_version(key)

    @override
    async def read_snapshot_bounded(self, key: str, max_bytes: int) -> StorageSnapshot:
        data = await self.read_bytes_bounded(key, max_bytes)
        return StorageSnapshot(data, await self.get_version(key), content_hash_bytes(data))

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


async def test_storage_document_without_enterprise_tenant_never_opens_storage(monkeypatch):
    from app.services.agent_tool_exec import documents

    def forbidden():
        pytest.fail("tenant authorization must happen before storage access")

    monkeypatch.setattr("app.services.storage.get_storage_backend", forbidden)

    result = await documents._read_document_from_storage(uuid.uuid4(), "enterprise_info/report.docx")

    assert result == "File not found: enterprise_info/report.docx"


@pytest.mark.parametrize("source", ["workspace", "enterprise"])
@pytest.mark.parametrize("kind", ["missing", "directory"])
async def test_storage_document_preserves_missing_and_directory_errors(monkeypatch, source, kind):
    from app.services.agent_tool_exec import documents

    agent_id = uuid.uuid4()
    tenant_id = str(uuid.uuid4())
    rel = "enterprise_info/report.docx" if source == "enterprise" else "workspace/report.docx"
    key = f"enterprise_info_{tenant_id}/report.docx" if source == "enterprise" else f"{agent_id}/{rel}"
    backend = MemoryStorageBackend({f"{key}/child": b"data"} if kind == "directory" else {})

    async def forbidden(*_args):
        pytest.fail("missing files and directories must not be read")

    monkeypatch.setattr(backend, "read_bytes_bounded", forbidden)
    monkeypatch.setattr("app.services.storage.get_storage_backend", lambda: backend)

    result = await documents._read_document_from_storage(agent_id, rel, tenant_id=tenant_id)

    expected = "Path is a directory, not a document" if kind == "directory" else "File not found"
    assert result == f"{expected}: {rel}"


async def test_storage_document_cancellation_during_bounded_read_never_starts_parser(monkeypatch):
    from app.services.agent_tool_exec import document_reading, documents

    agent_id = uuid.uuid4()
    backend = MemoryStorageBackend({f"{agent_id}/workspace/report.docx": b"document"})
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def read(key: str, max_bytes: int) -> bytes:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return b"document"

    async def forbidden(*_args, **_kwargs):
        pytest.fail("a cancelled read must never launch the parser")

    monkeypatch.setattr(backend, "read_bytes_bounded", read)
    monkeypatch.setattr(document_reading, "convert_document_isolated_async", forbidden)
    monkeypatch.setattr("app.services.storage.get_storage_backend", lambda: backend)
    task = asyncio.create_task(documents._read_document_from_storage(agent_id, "workspace/report.docx"))
    try:
        await asyncio.wait_for(started.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert cancelled.is_set()


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
    assert (
        search.split("\nSearch page: ")[0]
        == "🔍 Found 1+ match(es) in 1 file(s) for pattern 'needle':\nworkspace/notes.md:1: needle"
    )
    assert _search_metadata(search).complete
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
        "status": FocusStatus.IN_PROGRESS,
        "kind": FocusKind.NORMAL,
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


async def test_flush_skips_escaping_symlink_and_writes_other_files(monkeypatch, tmp_path):
    from app.services.agent_tool_exec import workspace_temp

    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend()
    monkeypatch.setattr(workspace_temp, "get_storage_backend", lambda: storage)
    temp_ws = await workspace_temp._prepare_temp_workspace(agent_id, paths=["workspace"])
    try:
        outside = tmp_path / "secret.md"
        outside.write_text("secret")
        (temp_ws.root / "workspace/link.md").symlink_to(outside)
        (temp_ws.root / "workspace/good.md").write_text("good")
        result = await workspace_temp.flush_temp_workspace(temp_ws)
    finally:
        temp_ws.cleanup()

    assert result["updated"] == ["workspace/good.md"]
    assert storage.files == {f"{agent_id}/workspace/good.md": b"good"}


async def test_flush_skips_escaping_symlink_and_flushes_other_files(monkeypatch, tmp_path):
    from app.services.agent_tool_exec import workspace_temp

    agent_id = uuid.uuid4()
    backend = MemoryStorageBackend({f"{agent_id}/workspace/input.md": b"old"})
    monkeypatch.setattr(workspace_temp, "get_storage_backend", lambda: backend)
    temp = await workspace_temp._prepare_temp_workspace(agent_id, paths=["workspace"])
    try:
        (temp.root / "workspace/input.md").write_bytes(b"new")
        secret = tmp_path / "secret"
        secret.write_bytes(b"secret")
        (temp.root / "workspace/escape").symlink_to(secret)
        result = await workspace_temp.flush_temp_workspace(temp)
    finally:
        temp.cleanup()
    assert result["updated"] == ["workspace/input.md"]
    assert backend.files[f"{agent_id}/workspace/input.md"] == b"new"
    assert f"{agent_id}/workspace/escape" not in backend.files


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


def _build_symlink_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Agent workspace with inside/outside symlinks; the outside target is a prefix sibling of the workspace."""
    workspace = tmp_path / "agent"
    sibling = tmp_path / "agent-other"
    docs = workspace / "workspace"
    docs.mkdir(parents=True)
    sibling.mkdir()
    (sibling / "secret.md").write_text("needle outside\n", encoding="utf-8")
    (sibling / "nested.md").write_text("needle nested outside\n", encoding="utf-8")
    (docs / "notes.md").write_text("needle inside\n", encoding="utf-8")
    (docs / "inside-file.md").symlink_to(docs / "notes.md")
    (docs / "real-dir").mkdir()
    (docs / "real-dir" / "child.md").write_text("needle child\n", encoding="utf-8")
    (docs / "inside-dir").symlink_to(docs / "real-dir", target_is_directory=True)
    (docs / "outside-file.md").symlink_to(sibling / "secret.md")
    (docs / "outside-dir").symlink_to(sibling, target_is_directory=True)
    return workspace, sibling


def _forbid_outside_access(monkeypatch: pytest.MonkeyPatch, outside: Path) -> None:
    """Fail the test when content, size or children of anything under ``outside`` are read."""
    read_text, iterdir, stat = Path.read_text, Path.iterdir, Path.stat

    def is_outside(path: Path) -> bool:
        return path.resolve().is_relative_to(outside.resolve())

    def guarded_read_text(self: Path, *args, **kwargs):
        assert not is_outside(self), f"read content of {self}"
        return read_text(self, *args, **kwargs)

    def guarded_iterdir(self: Path):
        assert not is_outside(self), f"listed children of {self}"
        return iterdir(self)

    def guarded_stat(self: Path, *args, **kwargs):
        assert not is_outside(self), f"stat of {self}"
        return stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
    monkeypatch.setattr(Path, "stat", guarded_stat)


def test_source_path_fallback_rejects_symlink_sibling_resolving_outside_root(monkeypatch, tmp_path):
    # Given: "secretnote.md" does not exist, so the normalized-name fallback matches "secret note.md" -> outside file
    workspace, sibling = _build_symlink_fixture(tmp_path)
    docs = workspace / "workspace"
    (docs / "secret note.md").symlink_to(sibling / "secret.md")
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    # When / Then
    with pytest.raises(ValueError, match="Access denied for this path"):
        agent_tools._resolve_tool_source_path(workspace, "workspace/secretnote.md")
    assert agent_tools._read_file(workspace, "workspace/secretnote.md") == "Access denied for this path"


def test_source_path_fallback_keeps_symlink_sibling_resolving_inside_root(monkeypatch, tmp_path):
    workspace, _sibling = _build_symlink_fixture(tmp_path)
    docs = workspace / "workspace"
    (docs / "my notes.md").symlink_to(docs / "notes.md")
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    source = agent_tools._resolve_tool_source_path(workspace, "workspace/mynotes.md")
    read = agent_tools._read_file(workspace, "workspace/mynotes.md")

    assert source == (docs / "notes.md").resolve()
    assert "needle inside" in read


def test_source_path_fallback_prefers_inside_match_over_escaping_match(monkeypatch, tmp_path):
    workspace, sibling = _build_symlink_fixture(tmp_path)
    docs = workspace / "workspace"
    (docs / "a b.md").symlink_to(sibling / "secret.md")
    (docs / "ab.MD").write_text("inside", encoding="utf-8")
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    assert agent_tools._resolve_tool_source_path(workspace, "workspace/a  b.md") == (docs / "ab.MD").resolve()


def test_local_search_skips_entries_resolving_outside_root_without_reading_them(monkeypatch, tmp_path):
    workspace, sibling = _build_symlink_fixture(tmp_path)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    _forbid_outside_access(monkeypatch, sibling)

    through_directory_link = agent_tools._search_files(workspace, "needle", path="workspace", file_pattern="*/*.md")
    everything = agent_tools._search_files(workspace, "needle", path="workspace")

    assert "outside" not in through_directory_link
    assert "outside" not in everything
    assert "workspace/notes.md:1: needle inside" in everything
    assert "workspace/inside-file.md:1: needle inside" in everything
    assert "workspace/real-dir/child.md:1: needle child" in everything
    assert "workspace/inside-dir/child.md:1: needle child" in through_directory_link
    # notes.md, inside-file.md, real-dir/child.md; the two outside links are not even counted as searched
    assert everything.startswith("🔍 Found 3+ match(es) in 3 file(s)")


def test_local_find_skips_entries_resolving_outside_root_without_statting_them(monkeypatch, tmp_path):
    workspace, sibling = _build_symlink_fixture(tmp_path)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    _forbid_outside_access(monkeypatch, sibling)

    everything = agent_tools._find_files(workspace, "*", path="workspace")
    through_directory_link = agent_tools._find_files(workspace, "outside-dir/*", path="workspace")
    only_outside = agent_tools._find_files(workspace, "outside-*", path="workspace")

    assert "outside" not in everything
    assert "📄 workspace/inside-file.md (14B)" in everything
    assert "📁 workspace/inside-dir/" in everything
    assert everything.startswith("📂 Found 4 item(s) (2 dirs, 2 files) matching '*':")
    assert through_directory_link == "No files matching pattern: outside-dir/*"
    assert only_outside == "No files matching pattern: outside-*"


def test_local_listing_skips_entries_resolving_outside_root_without_reading_them(monkeypatch, tmp_path):
    workspace, sibling = _build_symlink_fixture(tmp_path)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    _forbid_outside_access(monkeypatch, sibling)

    listing = agent_tools._list_files(workspace, "workspace")

    assert "outside" not in listing
    assert listing == (
        "📂 workspace: 2 folder(s), 2 file(s)\n"
        "  📁 inside-dir/ (1 items)\n"
        "  📄 inside-file.md (14B)\n"
        "  📄 notes.md (14B)\n"
        "  📁 real-dir/ (1 items)"
    )


def test_enterprise_root_search_find_and_listing_skip_symlinks_to_other_tenant(monkeypatch, tmp_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    enterprise = tmp_path / "enterprise_info_tenant-a"
    other_tenant = tmp_path / "enterprise_info_tenant-a-other"
    enterprise.mkdir()
    other_tenant.mkdir()
    (other_tenant / "payroll.md").write_text("needle payroll\n", encoding="utf-8")
    (enterprise / "handbook.md").write_text("needle handbook\n", encoding="utf-8")
    (enterprise / "handbook-link.md").symlink_to(enterprise / "handbook.md")
    (enterprise / "payroll-link.md").symlink_to(other_tenant / "payroll.md")
    (enterprise / "other-tenant").symlink_to(other_tenant, target_is_directory=True)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    _forbid_outside_access(monkeypatch, other_tenant)

    listing = agent_tools._list_files(workspace, "enterprise_info", tenant_id="tenant-a")
    search = agent_tools._search_files(workspace, "needle", path="enterprise_info", tenant_id="tenant-a")
    found = agent_tools._find_files(workspace, "*", path="enterprise_info", tenant_id="tenant-a")

    assert "payroll" not in listing + search + found
    assert "other-tenant" not in listing + search + found
    assert listing == "📂 enterprise_info: 0 folder(s), 2 file(s)\n  📄 handbook-link.md (16B)\n  📄 handbook.md (16B)"
    assert search.startswith("🔍 Found 2+ match(es) in 2 file(s)")
    assert found.startswith("📂 Found 2 item(s) (0 dirs, 2 files) matching '*':")


class CountingStorageBackend(MemoryStorageBackend):
    def __init__(self, files: dict[str, bytes]):
        super().__init__(files)
        self.calls: list[tuple[str, str]] = []

    @override
    async def exists(self, key: str) -> bool:
        self.calls.append(("exists", key))
        return await super().exists(key)

    @override
    async def is_dir(self, key: str) -> bool:
        self.calls.append(("is_dir", key))
        return await super().is_dir(key)

    @override
    async def list_dir(self, key: str) -> list[StorageEntry]:
        self.calls.append(("list_dir", key))
        return await super().list_dir(key)

    def listed(self) -> list[str]:
        return [key for operation, key in self.calls if operation == "list_dir"]


async def test_search_files_stops_listing_directories_once_it_holds_fifty_matches(monkeypatch):
    # Given
    agent_id = uuid.uuid4()
    backend = CountingStorageBackend(
        {f"{agent_id}/workspace/d{index:02d}/notes.txt": b"needle\n" * 3 for index in range(30)}
    )
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: backend)
    # When
    result = await agent_tools._storage_search_files(agent_id, "needle", path="workspace")
    # Then
    assert "Found 50+ match(es) in 17 file(s)" in result
    assert backend.listed() == [
        key for index in range(17) for key in (f"{agent_id}/workspace", f"{agent_id}/workspace/d{index:02d}")
    ]


async def test_find_files_reports_the_exact_total_but_keeps_only_one_hundred_rows(monkeypatch):
    # Given
    agent_id = uuid.uuid4()
    files = {f"{agent_id}/workspace/d{d}/f{f:03d}.md": b"x" for d in range(3) for f in range(50)}
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: MemoryStorageBackend(files))
    # When
    found = await agent_tools._storage_find_files(agent_id, "*.md", path="workspace")
    # Then
    header, *rows = found.splitlines()
    assert header == "📂 Found 150 item(s) (0 dirs, 100 files) matching '*.md':"
    assert len(rows) == 100


async def test_list_dir_checks_the_directory_once_and_lists_each_child_once(monkeypatch):
    # Given
    agent_id = uuid.uuid4()
    backend = CountingStorageBackend(
        {f"{agent_id}/workspace/a/x.md": b"x", f"{agent_id}/workspace/b/y.md": b"y", f"{agent_id}/workspace/z.md": b"z"}
    )
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: backend)
    # When
    listing = await agent_tools._storage_list_dir(agent_id, "workspace")
    # Then
    assert listing.startswith("📂 workspace: 2 folder(s), 1 file(s)")
    assert [operation for operation, _key in backend.calls if operation != "list_dir"] == ["is_dir"]
    assert backend.listed() == [f"{agent_id}/workspace", f"{agent_id}/workspace/a", f"{agent_id}/workspace/b"]


async def test_search_files_cancelled_while_listing_requests_no_further_directories(monkeypatch):
    # Given
    agent_id = uuid.uuid4()
    blocked = asyncio.Event()

    class StallingBackend(CountingStorageBackend):
        @override
        async def list_dir(self, key: str) -> list[StorageEntry]:
            if len(self.listed()) == 1:
                self.calls.append(("list_dir", key))
                blocked.set()
                await asyncio.Event().wait()
            return await super().list_dir(key)

    backend = StallingBackend({f"{agent_id}/workspace/d{index}/notes.txt": b"x" for index in range(5)})
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: backend)
    task = asyncio.create_task(agent_tools._storage_search_files(agent_id, "needle", path="workspace"))
    # When
    await asyncio.wait_for(blocked.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Then
    assert backend.listed() == [f"{agent_id}/workspace", f"{agent_id}/workspace/d0"]


class _SearchMetadata(BaseModel):
    exhausted: bool
    complete: bool
    stop_reason: str
    next_cursor: str | None
    unreadable_files: int
    issues: list[str]
    entries_scanned: int
    content_bytes: int
    lines_scanned: int


def _search_metadata(result: str) -> _SearchMetadata:
    return _SearchMetadata.model_validate_json(result.rsplit("\nSearch page: ", 1)[1])


async def _registered_search(agent_id: uuid.UUID, arguments: ToolArguments, tenant: str | None = "tenant-a") -> str:
    handler = resolve("search_files")
    assert handler is not None
    with use_execution_context(ToolExecutionContext(tenant_id=tenant, workspace_root=agent_tools.WORKSPACE_ROOT)):
        result = handler(arguments=arguments, agent_id=agent_id, user_id=agent_id, session_id="search", on_output=None)
        return result if isinstance(result, str) else await result


class _SearchStorage(MemoryStorageBackend):
    def __init__(self):
        super().__init__()
        self.reads: list[str] = []

    @override
    async def read_bytes(self, key: str) -> bytes:
        raise AssertionError("search must never issue an unbounded read")

    @override
    async def read_bytes_bounded(self, key: str, max_bytes: int, *, offset: int | None = None) -> bytes:
        self.reads.append(key)
        return await super().read_bytes_bounded(key, max_bytes, offset=offset)


@pytest.fixture(autouse=True)
def _isolate_search_spill(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from app.services.agent_tool_exec import workspace_search_spill

    monkeypatch.setattr(workspace_search_spill, "_cache_path", lambda: tmp_path / "private-search" / "lines.sqlite3")


@pytest.fixture(params=["local", "object"])
def search_backend(request, monkeypatch, tmp_path) -> tuple[uuid.UUID, StorageBackend]:
    agent_id = uuid.uuid4()
    backend = LocalStorageBackend(str(tmp_path)) if request.param == "local" else _SearchStorage()
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: backend)
    return agent_id, backend


@pytest.mark.parametrize("count", [0, 1, 49, 50, 51, 100, 123])
async def test_registered_search_returns_each_line_once_and_exhausts(search_backend, count):
    agent_id, backend = search_backend
    key = f"{agent_id}/workspace/notes.md"
    await backend.write_bytes(key, "".join(f"needle {index}\n" for index in range(count)).encode())
    arguments: ToolArguments = {"pattern": "needle", "path": "workspace"}
    rows: list[str] = []
    pages: list[_SearchMetadata] = []

    for _ in range(5):
        result = await _registered_search(agent_id, arguments)
        metadata = _search_metadata(result)
        rows.extend(line for line in result.splitlines() if line.startswith("workspace/"))
        pages.append(metadata)
        if metadata.next_cursor is None:
            break
        arguments["cursor"] = metadata.next_cursor

    assert rows == [f"workspace/notes.md:{index + 1}: needle {index}" for index in range(count)]
    assert len(pages) == max(1, (count + 49) // 50)
    assert pages[-1].complete
    assert pages[-1].exhausted
    assert all(page.stop_reason == "result_limit" for page in pages[:-1])
    if isinstance(backend, _SearchStorage):
        assert backend.reads == [key]


@pytest.mark.parametrize(
    "separator", ["\n", "\r\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
)
async def test_registered_search_keeps_multibyte_and_line_positions(search_backend, separator):
    agent_id, backend = search_backend
    await backend.write_bytes(
        f"{agent_id}/workspace/notes.md", separator.join(["é中 needle", "", "needle 終"]).encode()
    )
    first = await _registered_search(agent_id, {"pattern": "^.*needle", "path": "workspace", "limit": 1})
    cursor = _search_metadata(first).next_cursor
    assert cursor is not None
    second = await _registered_search(
        agent_id, {"pattern": "^.*needle", "path": "workspace", "limit": 1, "cursor": cursor}
    )

    assert "workspace/notes.md:1: é中 needle" in first
    assert "workspace/notes.md:3: needle 終" in second
    assert _search_metadata(second).complete


async def test_registered_search_scan_budget_can_return_an_empty_progressing_page(search_backend):
    from app.services.agent_tool_exec.workspace_search_cursor import SCAN_STEPS

    agent_id, backend = search_backend
    for index in range(SCAN_STEPS + 1):
        await backend.write_bytes(
            f"{agent_id}/workspace/f{index:04}.txt", b"needle" if index == SCAN_STEPS else b"other"
        )

    first = await _registered_search(agent_id, {"pattern": "needle", "path": "workspace"})
    metadata = _search_metadata(first)
    assert metadata.next_cursor is not None
    second = await _registered_search(
        agent_id, {"pattern": "needle", "path": "workspace", "cursor": metadata.next_cursor}
    )

    assert metadata.stop_reason == "scan_budget"
    assert not metadata.exhausted
    assert not metadata.complete
    assert metadata.entries_scanned == SCAN_STEPS
    assert not any(line.startswith("workspace/") for line in first.splitlines())
    assert f"workspace/f{SCAN_STEPS:04}.txt:1: needle" in second
    assert _search_metadata(second).complete


async def test_registered_search_content_budget_and_file_size_boundary(search_backend):
    from app.services.agent_tool_exec.workspace_search_cursor import CONTENT_BYTES, FILE_BYTES

    agent_id, backend = search_backend
    for index in range(5):
        await backend.write_bytes(f"{agent_id}/workspace/{index}.md", b"x" * (FILE_BYTES - 7) + b"needle\n")
    await backend.write_bytes(f"{agent_id}/workspace/large.md", b"x" * (FILE_BYTES + 1) + b"needle")
    rows: list[str] = []
    cursor: str | None = None
    reasons: list[str] = []
    pages: list[_SearchMetadata] = []
    for _ in range(4):
        arguments: ToolArguments = {"pattern": "needle", "path": "workspace"}
        if cursor is not None:
            arguments["cursor"] = cursor
        result = await _registered_search(agent_id, arguments)
        metadata = _search_metadata(result)
        pages.append(metadata)
        rows.extend(line for line in result.splitlines() if line.startswith("workspace/"))
        reasons.append(metadata.stop_reason)
        assert metadata.content_bytes <= CONTENT_BYTES
        cursor = metadata.next_cursor
        if cursor is None:
            break

    assert len(rows) == 6
    assert reasons == ["content_budget", "exhausted"]
    metadata = pages[-1]
    assert metadata.exhausted
    assert metadata.complete
    assert metadata.unreadable_files == 0
    assert metadata.issues == []


async def test_registered_search_line_budget_preserves_unfinished_file(search_backend):
    from app.services.agent_tool_exec.workspace_search_cursor import LINE_STEPS

    agent_id, backend = search_backend
    await backend.write_bytes(f"{agent_id}/workspace/lines.md", b"\n" * LINE_STEPS + b"needle")
    first = await _registered_search(agent_id, {"pattern": "needle", "path": "workspace"})
    metadata = _search_metadata(first)
    assert metadata.next_cursor is not None
    second = await _registered_search(
        agent_id, {"pattern": "needle", "path": "workspace", "cursor": metadata.next_cursor}
    )

    assert metadata.stop_reason == "line_budget"
    assert metadata.lines_scanned == LINE_STEPS
    assert f"workspace/lines.md:{LINE_STEPS + 1}: needle" in second
    assert _search_metadata(second).complete


async def test_registered_search_preserves_captured_text_but_reads_unvisited_changes(search_backend):
    agent_id, backend = search_backend
    await backend.write_bytes(f"{agent_id}/workspace/a.md", b"needle old1\nneedle old2\n")
    await backend.write_bytes(f"{agent_id}/workspace/b.md", b"needle before\n")
    first = await _registered_search(agent_id, {"pattern": "needle", "path": "workspace", "limit": 1})
    await backend.delete(f"{agent_id}/workspace/a.md")
    await backend.write_bytes(f"{agent_id}/workspace/b.md", b"needle after\n")
    cursor = _search_metadata(first).next_cursor
    assert cursor is not None
    second = await _registered_search(
        agent_id, {"pattern": "needle", "path": "workspace", "limit": 1, "cursor": cursor}
    )
    cursor = _search_metadata(second).next_cursor
    assert cursor is not None
    third = await _registered_search(agent_id, {"pattern": "needle", "path": "workspace", "limit": 1, "cursor": cursor})

    assert "workspace/a.md:2: needle old2" in second
    assert "workspace/b.md:1: needle after" in third
    assert _search_metadata(third).complete


@pytest.mark.parametrize(
    "change", ["agent", "tenant", "path", "pattern", "file_pattern", "ignore_case", "limit", "tamper"]
)
async def test_registered_search_cursor_is_bound_and_authenticated(search_backend, change):
    agent_id, backend = search_backend
    await backend.write_bytes(f"{agent_id}/workspace/a.md", b"needle\nneedle\n")
    await backend.write_bytes(f"{agent_id}/other/a.md", b"needle\n")
    first = await _registered_search(agent_id, {"pattern": "needle", "path": "workspace", "limit": 1})
    cursor = _search_metadata(first).next_cursor
    assert cursor is not None
    arguments: ToolArguments = {"pattern": "needle", "path": "workspace", "limit": 1, "cursor": cursor}
    if change in ("path", "pattern", "file_pattern", "ignore_case", "limit"):
        arguments[change] = {
            "path": "other",
            "pattern": "other",
            "file_pattern": "*.txt",
            "ignore_case": True,
            "limit": 2,
        }[change]
    if change == "tamper":
        arguments["cursor"] = cursor[:20] + ("A" if cursor[20] != "A" else "B") + cursor[21:]
    foreign_agent = uuid.uuid4() if change == "agent" else agent_id
    if change == "agent":
        await backend.write_bytes(f"{foreign_agent}/workspace/a.md", b"needle")

    result = await _registered_search(foreign_agent, arguments, "tenant-b" if change == "tenant" else "tenant-a")

    assert result.startswith("Invalid search_files cursor:")


@pytest.mark.parametrize(
    "arguments",
    [
        {"cursor": ""},
        {"cursor": "garbage"},
        {"cursor": None},
        {"cursor": 1},
        {"limit": 0},
        {"limit": 51},
        {"limit": True},
        {"limit": "1"},
        {"limit": 1.5},
    ],
)
async def test_registered_search_rejects_invalid_continuation(search_backend, arguments):
    agent_id, backend = search_backend
    await backend.write_bytes(f"{agent_id}/workspace/a.md", b"needle")

    result = await _registered_search(agent_id, {"pattern": "needle", "path": "workspace", **arguments})

    assert result.startswith("Invalid search_files")


async def test_search_catalogs_advertise_equivalent_continuation_schema():
    from app.services.agent_tools_definitions import AGENT_TOOLS
    from app.services.tool_definitions.file import FILE_TOOLS

    seeded = next(tool for tool in FILE_TOOLS if tool["name"] == "search_files")["parameters_schema"]
    runtime = next(tool["function"] for tool in AGENT_TOOLS if tool["function"]["name"] == "search_files")["parameters"]

    for name in ("cursor", "limit"):
        assert seeded["properties"][name] == runtime["properties"][name]
    assert runtime["properties"]["limit"]["maximum"] == 50


@pytest.mark.parametrize("filename", ["*.md", "workspace/a/nested/*.md", "*"])
async def test_registered_search_resumes_nested_traversal_with_filename_and_case_selection(search_backend, filename):
    agent_id, backend = search_backend
    for name in (
        "a/nested/first.md",
        "a/nested/last.md",
        "z.md",
        "skip.txt",
        ".hidden.md",
        ".hidden/skip.md",
        "image.PNG",
    ):
        await backend.write_bytes(f"{agent_id}/workspace/{name}", b"NEEDLE\n")
    await backend.write_bytes(f"{agent_id}/workspace/no.md", b"not matched")
    arguments: ToolArguments = {
        "pattern": "^needle$",
        "path": "workspace",
        "file_pattern": filename,
        "ignore_case": True,
        "limit": 1,
    }
    rows: list[str] = []
    for _ in range(5):
        result = await _registered_search(agent_id, arguments)
        rows.extend(line for line in result.splitlines() if line.startswith("workspace/"))
        metadata = _search_metadata(result)
        if metadata.next_cursor is None:
            break
        arguments["cursor"] = metadata.next_cursor

    expected = ["workspace/a/nested/first.md:1: NEEDLE", "workspace/a/nested/last.md:1: NEEDLE"]
    if filename == "*":
        expected.append("workspace/skip.txt:1: NEEDLE")
    if filename in ("*.md", "*"):
        expected.append("workspace/z.md:1: NEEDLE")
    assert rows == expected
    sensitive = await _registered_search(agent_id, {"pattern": "needle", "path": "workspace"})
    assert not any(line.startswith("workspace/") for line in sensitive.splitlines())
    assert _search_metadata(sensitive).complete


async def test_registered_search_unreadable_file_is_incomplete_but_traversal_continues(
    search_backend: tuple[uuid.UUID, StorageBackend], monkeypatch
):
    agent_id, backend = search_backend
    for name in ("a.md", "b.md"):
        await backend.write_bytes(f"{agent_id}/workspace/{name}", b"needle\n")
    original_read = backend.read_bytes_bounded

    async def interrupted_read(key: str, max_bytes: int, *, offset: int | None = None) -> bytes:
        if key.endswith("/a.md"):
            raise FileNotFoundError(key)
        return await original_read(key, max_bytes, offset=offset)

    monkeypatch.setattr(backend, "read_bytes_bounded", interrupted_read)
    result = await _registered_search(agent_id, {"pattern": "needle", "path": "workspace"})

    metadata = _search_metadata(result)
    assert metadata.exhausted
    assert not metadata.complete
    assert metadata.unreadable_files == 1
    assert metadata.issues == ["workspace/a.md: content unreadable"]
    assert "workspace/b.md:1: needle" in result


@pytest.mark.parametrize("operation", ["listing", "read"])
async def test_registered_search_cancellation_closes_owned_operation_and_never_reads_next_entry(
    search_backend: tuple[uuid.UUID, StorageBackend], monkeypatch, operation
):
    import anyio

    agent_id, backend = search_backend
    await backend.write_bytes(f"{agent_id}/workspace/a.md", b"needle")
    await backend.write_bytes(f"{agent_id}/workspace/b.md", b"needle")
    started = anyio.Event()
    closed = anyio.Event()
    reads: list[str] = []
    original_read = backend.read_bytes_bounded

    async def bounded_read(key: str, max_bytes: int, *, offset: int | None = None) -> bytes:
        reads.append(key)
        if operation == "read":
            started.set()
            try:
                await anyio.sleep_forever()
            finally:
                closed.set()
        return await original_read(key, max_bytes, offset=offset)

    async def listing(key: str, *, limit: int, after: str = "") -> StoragePage:
        del key, limit, after
        started.set()
        try:
            await anyio.sleep_forever()
        finally:
            closed.set()
        raise AssertionError("cancelled operation must not return")

    monkeypatch.setattr(backend, "read_bytes_bounded", bounded_read)
    if operation == "listing":
        monkeypatch.setattr(backend, "list_dir_page", listing)
    async with anyio.create_task_group() as group:
        arguments: ToolArguments = {"pattern": "needle", "path": "workspace"}
        group.start_soon(_registered_search, agent_id, arguments, "tenant-a")
        with anyio.fail_after(5):
            await started.wait()
        group.cancel_scope.cancel()

    assert closed.is_set()
    assert reads == ([] if operation == "listing" else [f"{agent_id}/workspace/a.md"])


@pytest.mark.parametrize("source", ["workspace", "enterprise_info"])
async def test_registered_search_local_symlinks_and_parent_traversal_never_escape(monkeypatch, tmp_path, source):
    agent_id = uuid.uuid4()
    backend = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: backend)
    root = tmp_path / (str(agent_id) if source == "workspace" else "enterprise_info_tenant-a")
    root.mkdir()
    outside = tmp_path / (f"{agent_id}-other" if source == "workspace" else "enterprise_info_tenant-b")
    outside.mkdir()
    (outside / "secret.md").write_text("needle SECRET")
    (root / "safe.md").write_text("needle safe\nneedle again")
    (root / "escaped.md").symlink_to(outside / "secret.md")
    (root / "escaped-dir").symlink_to(outside, target_is_directory=True)
    path = "." if source == "workspace" else source

    first = await _registered_search(agent_id, {"pattern": "needle", "path": path, "limit": 1})
    cursor = _search_metadata(first).next_cursor
    assert cursor is not None
    second = await _registered_search(agent_id, {"pattern": "needle", "path": path, "limit": 1, "cursor": cursor})
    explicit = await _registered_search(
        agent_id, {"pattern": "needle", "path": ("" if path == "." else path + "/") + "escaped-dir"}
    )
    parent = await _registered_search(
        agent_id, {"pattern": "needle", "path": ("" if path == "." else "./" + path + "/") + "../" + outside.name}
    )

    assert "SECRET" not in first + second
    assert "safe.md:1: needle safe" in first
    assert "safe.md:2: needle again" in second
    assert _search_metadata(second).complete
    assert explicit == parent == "Access denied for this path"


async def test_registered_search_shared_scope_uses_context_tenant_and_rejects_missing_tenant(
    search_backend, monkeypatch
):
    agent_id, backend = search_backend
    await backend.write_bytes("enterprise_info_tenant-a/notes.md", b"needle allowed\nneedle again")
    await backend.write_bytes("enterprise_info_tenant-b/notes.md", b"needle FOREIGN")
    await backend.write_bytes("enterprise_info/notes.md", b"needle LEGACY")

    async def forbidden_lookup(_agent_id: uuid.UUID) -> str:
        pytest.fail("execution context must resolve the tenant without a DAO lookup")

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", forbidden_lookup)
    first = await _registered_search(agent_id, {"pattern": "needle", "path": "enterprise_info", "limit": 1})
    cursor = _search_metadata(first).next_cursor
    assert cursor is not None
    foreign = await _registered_search(
        agent_id, {"pattern": "needle", "path": "enterprise_info", "limit": 1, "cursor": cursor}, "tenant-b"
    )
    missing = await _registered_search(agent_id, {"pattern": "needle", "path": "enterprise_info"}, None)

    assert "enterprise_info/notes.md:1: needle allowed" in first
    assert foreign.startswith("Invalid search_files cursor:")
    assert missing == "Directory not found: enterprise_info"


async def test_registered_search_rejects_unknown_cursor_version(search_backend):
    from app.services.agent_tool_exec.workspace_search_cursor import SearchQuery, _cipher

    agent_id, backend = search_backend
    await backend.write_bytes(f"{agent_id}/workspace/a.md", b"needle")
    query = SearchQuery(
        agent=str(agent_id),
        tenant="tenant-a",
        root=f"{agent_id}/workspace",
        display="workspace",
        pattern="needle",
        file_pattern="*",
        ignore_case=False,
        limit=50,
    )
    cursor = (
        _cipher().encrypt(('{"version":2,"query":' + query.model_dump_json() + ',"directories":[]}').encode()).decode()
    )

    result = await _registered_search(agent_id, {"pattern": "needle", "path": "workspace", "cursor": cursor})

    assert result.startswith("Invalid search_files cursor:")


async def test_registered_search_fallback_ranges_bound_actual_reads_without_partial_promotion(monkeypatch):
    from app.services.agent_tool_exec.workspace_search_cursor import CONTENT_BYTES, FILE_BYTES

    class MeteredStorage(_SearchStorage):
        def __init__(self):
            super().__init__()
            self.bytes_read: int = 0

        @override
        async def read_bytes_bounded(self, key: str, max_bytes: int, *, offset: int | None = None) -> bytes:
            self.bytes_read += min(len(self.files.get(key, b"")[(offset or 0) :]), max_bytes + (offset is None))
            return await super().read_bytes_bounded(key, max_bytes, offset=offset)

        @override
        async def create_bytes_if_absent(
            self, key: str, data: bytes, content_type: str | None = None
        ) -> ConditionalWriteResult:
            if key in self.files:
                return ConditionalWriteResult(ok=False, conflict=True)
            await self.write_bytes(key, data, content_type)
            return ConditionalWriteResult(ok=True)

    agent_id = uuid.uuid4()
    primary, legacy = MeteredStorage(), MeteredStorage()
    backend = FallbackStorageBackend(primary, legacy)
    for index in range(3):
        await legacy.write_bytes(f"{agent_id}/workspace/{index}.md", b"needle\n" + b"x" * (FILE_BYTES - 7))
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: backend)
    arguments: ToolArguments = {"pattern": "needle", "path": "workspace"}
    rows: list[str] = []
    for _ in range(3):
        before = primary.bytes_read + legacy.bytes_read
        result = await _registered_search(agent_id, arguments)
        metadata = _search_metadata(result)
        actual_bytes = primary.bytes_read + legacy.bytes_read - before
        rows.extend(line for line in result.splitlines() if line.startswith("workspace/"))
        assert actual_bytes <= metadata.content_bytes <= CONTENT_BYTES
        if metadata.next_cursor is None:
            assert metadata.complete
            break
        arguments["cursor"] = metadata.next_cursor

    assert rows == [f"workspace/{index}.md:1: needle" for index in range(3)]
    assert primary.files == {}


async def test_registered_search_large_file_streams_every_line_once_with_bounded_ranges(
    search_backend: tuple[uuid.UUID, StorageBackend], monkeypatch: pytest.MonkeyPatch
):
    from app.services.agent_tool_exec.workspace_search_cursor import CONTENT_BYTES, FILE_BYTES

    agent_id, backend = search_backend
    key = f"{agent_id}/workspace/large.md"
    count = 151
    line = "é中 needle " + "x" * 20_000
    data = ((line + "\r\n") * count).encode()
    await backend.write_bytes(key, data)
    original = backend.read_bytes_bounded
    ranges: list[tuple[int, int]] = []

    async def read_range(key: str, max_bytes: int, *, offset: int | None = None) -> bytes:
        assert offset is not None
        assert max_bytes <= FILE_BYTES
        chunk = await original(key, max_bytes, offset=offset)
        ranges.append((offset, len(chunk)))
        return chunk

    monkeypatch.setattr(backend, "read_bytes_bounded", read_range)
    arguments: ToolArguments = {"pattern": "^é中 needle x+$", "path": "workspace"}
    rows: list[str] = []
    cursors: list[str] = []
    for _ in range(10):
        result = await _registered_search(agent_id, arguments)
        metadata = _search_metadata(result)
        assert metadata.content_bytes <= CONTENT_BYTES
        rows.extend(row for row in result.splitlines() if row.startswith("workspace/"))
        if metadata.next_cursor is None:
            assert metadata.complete
            break
        cursors.append(metadata.next_cursor)
        arguments["cursor"] = metadata.next_cursor

    assert rows == [f"workspace/large.md:{number}: {line[:100]}" for number in range(1, count + 1)]
    assert sum(size for _, size in ranges) == len(data)
    assert [offset for offset, _ in ranges] == list(range(0, len(data), FILE_BYTES))
    assert len(cursors) >= 3


@pytest.mark.parametrize("ending", ["", "\r\n", "\u2028"])
async def test_registered_search_long_line_spills_and_replays_without_file_sized_cursor(search_backend, ending):
    from app.services.agent_tool_exec.workspace_search_cursor import CONTENT_BYTES, SearchPosition, _cipher

    agent_id, backend = search_backend
    long_line = "é" + "x" * (CONTENT_BYTES * 2 + 31) + "中needle"
    await backend.write_bytes(f"{agent_id}/workspace/long.md", (long_line + ending + "tail needle").encode())
    arguments: ToolArguments = {"pattern": r"^éx+中needle(?:tail needle)?$|^tail needle$", "path": "workspace"}
    first = await _registered_search(agent_id, arguments)
    metadata = _search_metadata(first)
    assert metadata.stop_reason == "content_budget"
    assert metadata.next_cursor is not None
    assert not any(row.startswith("workspace/") for row in first.splitlines())
    cursor = metadata.next_cursor
    captured = SearchPosition.model_validate_json(_cipher().decrypt(cursor.encode()))
    assert captured.pending is not None
    assert captured.pending.offset == CONTENT_BYTES
    assert captured.pending.fragments
    assert len(cursor) < 4096

    rows: list[str] = []
    result = first
    for _ in range(4):
        arguments["cursor"] = cursor
        result = await _registered_search(agent_id, arguments)
        page = _search_metadata(result)
        assert page.content_bytes <= CONTENT_BYTES
        rows.extend(row for row in result.splitlines() if row.startswith("workspace/"))
        if page.next_cursor is None:
            assert page.complete
            break
        cursor = page.next_cursor

    assert rows == [f"workspace/long.md:1: {long_line[:100]}"] + (
        ["workspace/long.md:2: tail needle"] if ending else []
    )
    replay = await _registered_search(agent_id, {**arguments, "cursor": cursor})
    replay_again = await _registered_search(agent_id, {**arguments, "cursor": cursor})
    assert replay.split("\nSearch page: ")[0] == result.split("\nSearch page: ")[0]
    assert _search_metadata(replay).complete
    assert _search_metadata(replay).content_bytes == _search_metadata(replay_again).content_bytes
    assert replay.split("\nSearch page: ")[0] == replay_again.split("\nSearch page: ")[0]


@pytest.mark.parametrize("boundary", ["utf8", "crlf", "unicode", "invalid_utf8", "eof"])
async def test_registered_search_window_boundaries_keep_decode_and_line_semantics(search_backend, boundary):
    from app.services.agent_tool_exec.workspace_search_cursor import FILE_BYTES

    agent_id, backend = search_backend
    suffix = {
        "utf8": "中needle\nneedle".encode(),
        "crlf": b"\r\nneedle",
        "unicode": "\u2028needle".encode(),
        "invalid_utf8": b"\xe4\xffneedle\nneedle",
        "eof": b"",
    }[boundary]
    prefix = b"x" * (FILE_BYTES - 1) if boundary != "eof" else b"x" * FILE_BYTES
    await backend.write_bytes(f"{agent_id}/workspace/boundary.md", prefix + suffix)
    result = await _registered_search(agent_id, {"pattern": "needle$|^x+$", "path": "workspace"})
    rows = [row for row in result.splitlines() if row.startswith("workspace/")]

    first = "workspace/boundary.md:1: " + "x" * 100
    assert rows == [first] + ([] if boundary == "eof" else ["workspace/boundary.md:2: needle"])
    assert _search_metadata(result).complete


async def test_registered_search_traversal_deeper_than_128_directories_resumes(search_backend):
    agent_id, backend = search_backend
    relative = "workspace/" + "d/" * 140 + "deep.md"
    await backend.write_bytes(f"{agent_id}/{relative}", b"needle")
    first = await _registered_search(agent_id, {"pattern": "needle", "path": "workspace"})
    cursor = _search_metadata(first).next_cursor
    assert cursor is not None
    result = await _registered_search(agent_id, {"pattern": "needle", "path": "workspace", "cursor": cursor})

    assert f"{relative}:1: needle" in result
    assert _search_metadata(result).complete


@pytest.mark.parametrize("cursor_limit", [None, 2048])
async def test_registered_search_deep_branched_tree_emits_consumable_cursors_through_exhaustion(
    monkeypatch: pytest.MonkeyPatch, cursor_limit: int | None
):
    from app.services.agent_tool_exec import workspace_search_cursor

    maximum = workspace_search_cursor.MAX_CURSOR_LENGTH if cursor_limit is None else cursor_limit
    monkeypatch.setattr(workspace_search_cursor, "MAX_CURSOR_LENGTH", maximum)

    agent_id = uuid.uuid4()
    backend = _SearchStorage()
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: backend)
    depth = 1600
    for level in range(depth + 1):
        key = f"{agent_id}/workspace/" + "d/" * level + "z.md"
        await backend.write_bytes(key, b"needle")
    arguments: ToolArguments = {"pattern": "needle", "path": "workspace"}
    rows: list[str] = []
    exhausted = False
    for _ in range(100):
        result = await _registered_search(agent_id, arguments)
        metadata = _search_metadata(result)
        rows.extend(row for row in result.splitlines() if row.startswith("workspace/"))
        if metadata.next_cursor is None:
            exhausted = True
            assert metadata.complete
            break
        assert len(metadata.next_cursor) <= maximum
        arguments["cursor"] = metadata.next_cursor

    assert exhausted
    assert rows == ["workspace/" + "d/" * level + "z.md:1: needle" for level in reversed(range(depth + 1))]


@pytest.mark.parametrize("change", ["overwrite", "truncate", "delete"])
async def test_registered_search_future_ranges_observe_current_bytes_at_saved_offsets(search_backend, change):
    from app.services.agent_tool_exec.workspace_search_cursor import CONTENT_BYTES

    agent_id, backend = search_backend
    key = f"{agent_id}/workspace/live.md"
    await backend.write_bytes(key, b"x" * CONTENT_BYTES + b"old needle")
    first = await _registered_search(agent_id, {"pattern": "^x+new needle$", "path": "workspace"})
    cursor = _search_metadata(first).next_cursor
    assert cursor is not None
    if change == "delete":
        await backend.delete(key)
    else:
        await backend.write_bytes(key, b"z" * CONTENT_BYTES + b"new needle" if change == "overwrite" else b"short")

    result = await _registered_search(agent_id, {"pattern": "^x+new needle$", "path": "workspace", "cursor": cursor})
    metadata = _search_metadata(result)
    rows = [row for row in result.splitlines() if row.startswith("workspace/")]

    assert rows == (["workspace/live.md:1: " + "x" * 100] if change == "overwrite" else [])
    assert metadata.exhausted
    assert metadata.complete == (change != "delete")
    assert metadata.unreadable_files == (1 if change == "delete" else 0)


@pytest.mark.parametrize("continuation_storage", ["inline", "stored"])
@pytest.mark.parametrize("failure", ["missing", "corrupt", "expired", "foreign"])
async def test_registered_search_spill_or_cursor_loss_fails_explicitly(
    search_backend, monkeypatch, failure, continuation_storage
):
    import sqlite3

    from app.services.agent_tool_exec import workspace_search_cursor, workspace_search_spill

    if continuation_storage == "stored":
        monkeypatch.setattr(workspace_search_cursor, "MAX_CURSOR_LENGTH", 512)
    agent_id, backend = search_backend
    await backend.write_bytes(
        f"{agent_id}/workspace/long.md", b"x" * (workspace_search_cursor.CONTENT_BYTES + 1) + b"needle"
    )
    first = await _registered_search(agent_id, {"pattern": "needle", "path": "workspace"})
    cursor = _search_metadata(first).next_cursor
    assert cursor is not None
    if failure == "expired":
        now = workspace_search_cursor.time()
        monkeypatch.setattr(workspace_search_cursor, "time", lambda: now + workspace_search_cursor.CURSOR_TTL + 1)
    elif failure != "foreign":
        with sqlite3.connect(workspace_search_spill._cache_path()) as connection:
            if failure == "missing":
                connection.execute("DELETE FROM fragments")
            else:
                connection.execute("UPDATE fragments SET payload = ?", (b"corrupt",))

    result = await _registered_search(
        agent_id, {"pattern": "foreign" if failure == "foreign" else "needle", "path": "workspace", "cursor": cursor}
    )

    assert result.startswith("Invalid search_files cursor:")
    assert "Search page:" not in result
