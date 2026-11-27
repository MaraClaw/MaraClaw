import asyncio
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import override

import pytest
from pydantic import BaseModel

from app.config import get_settings
from app.core.domain.focus import FocusKind, FocusStatus
from app.core.events import RedisPoolOverloadError
from app.dao.agent_dao import agent_dao
from app.services import activity_logger, agent_tools, workspace_collaboration, workspace_locking
from app.services.agent_tool_exec import (
    _agent_tool_exec_storage as storage,
    workspace,
    workspace_read,
    workspace_read_window,
)
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
    CopyRequest,
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
from app.services.workspace_locking import (
    WorkspaceLockLostError,
    check_workspace_lock,
    workspace_locks as real_workspace_locks,
)


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
            version = "" if tail else str(self.versions.get(existing, 0))
            entries[name] = StorageEntry(
                name=name,
                key=f"{prefix}{name}",
                is_dir=bool(tail),
                size=0 if tail else len(data),
                version_id=version,
                etag=version,
                content_hash=version,
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
    monkeypatch, tool_name: str, arguments: ToolArguments
) -> None:
    agent_id = uuid.uuid4()
    calls: list[tuple[str, ToolArguments, uuid.UUID, str | None]] = []

    async def mutation_owner(
        received_tool_name: str,
        received_arguments: ToolArguments,
        *,
        agent_id: uuid.UUID,
        session_id: str | None,
    ) -> str:
        calls.append((received_tool_name, received_arguments, agent_id, session_id))
        return "mutation result"

    async def legacy_owner(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("storage callbacks must not call legacy mutation wrappers")

    monkeypatch.setattr(workspace, "_execute_workspace_mutation", mutation_owner)
    monkeypatch.setattr(agent_tools, "_execute_workspace_mutation", legacy_owner)

    handler = getattr(storage, tool_name)
    handler_result = handler(
        arguments=arguments, agent_id=agent_id, user_id=agent_id, session_id="mutation", on_output=None
    )
    assert not isinstance(handler_result, str)
    assert await handler_result == "mutation result"
    assert calls == [(tool_name, arguments, agent_id, "mutation")]


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


@pytest.mark.asyncio
async def test_storage_listing_adds_the_enterprise_folder_at_the_root_and_skips_hidden_entries(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({f"{agent_id}/workspace/notes.md": b"first\n", f"{agent_id}/.hidden": b"hidden"})
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    listing = await agent_tools._storage_list_dir(agent_id, "", tenant_id="tenant-a")

    assert (
        listing
        == "📂 root: 2 folder(s), 0 file(s)\n  📁 enterprise_info/ (shared company info)\n  📁 workspace/ (1 items)"
    )


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("write_file", {"path": "enterprise_info/report.md", "content": "ignored"}),
        ("edit_file", {"path": "enterprise_info/report.md", "old_string": "a", "new_string": "b"}),
        ("delete_file", {"path": "enterprise_info/report.md"}),
        ("move_file", {"source_path": "workspace/a.md", "destination_path": "enterprise_info/a.md"}),
        ("move_file", {"source_path": "enterprise_info/a.md", "destination_path": "workspace/a.md"}),
    ],
)
@pytest.mark.asyncio
async def test_workspace_mutation_refuses_enterprise_info_paths_without_io(tool_name, arguments):
    result = await workspace._execute_workspace_mutation(tool_name, arguments, agent_id=uuid.uuid4(), session_id=None)

    assert result.startswith("❌")
    assert "enterprise_info" in result


@pytest.mark.asyncio
async def test_workspace_mutation_rejects_tasks_snapshot_move_without_io():
    result = await workspace._execute_workspace_mutation(
        "move_file",
        {"source_path": "tasks.json", "destination_path": "workspace/tasks.json"},
        agent_id=uuid.uuid4(),
        session_id=None,
    )

    # Then
    assert result == "❌ tasks.json cannot be moved (protected)"


@pytest.mark.parametrize(
    ("path", "expected"),
    [("../other/secret.md", "other/secret.md"), ("a/../../b.md", "b.md"), ("/abs/x.md", "abs/x.md"), ("..", "")],
)
def test_storage_mutations_keep_every_path_inside_the_agent_tree(path, expected):
    # write, edit and delete address `{agent_id}/{normalized}`, so a path that climbs can only land under the agent
    assert workspace_collaboration.normalize_workspace_path(path) == expected


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

    # Then
    assert source == nested_file
    assert target == workspace / "nested" / "draft.md"


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


def test_source_path_fallback_rejects_symlink_sibling_resolving_outside_root(monkeypatch, tmp_path):
    # Given: "secretnote.md" does not exist, so the normalized-name fallback matches "secret note.md" -> outside file
    workspace, sibling = _build_symlink_fixture(tmp_path)
    docs = workspace / "workspace"
    (docs / "secret note.md").symlink_to(sibling / "secret.md")
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    # When / Then
    with pytest.raises(ValueError, match="Access denied for this path"):
        agent_tools._resolve_tool_source_path(workspace, "workspace/secretnote.md")


def test_source_path_fallback_keeps_symlink_sibling_resolving_inside_root(monkeypatch, tmp_path):
    workspace, _sibling = _build_symlink_fixture(tmp_path)
    docs = workspace / "workspace"
    (docs / "my notes.md").symlink_to(docs / "notes.md")
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    source = agent_tools._resolve_tool_source_path(workspace, "workspace/mynotes.md")

    assert source == (docs / "notes.md").resolve()


def test_source_path_fallback_prefers_inside_match_over_escaping_match(monkeypatch, tmp_path):
    workspace, sibling = _build_symlink_fixture(tmp_path)
    docs = workspace / "workspace"
    (docs / "a b.md").symlink_to(sibling / "secret.md")
    (docs / "ab.MD").write_text("inside", encoding="utf-8")
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    assert agent_tools._resolve_tool_source_path(workspace, "workspace/a  b.md") == (docs / "ab.MD").resolve()


@pytest.mark.asyncio
async def test_storage_listing_of_a_local_workspace_never_shows_links_planted_to_another_agent(monkeypatch, tmp_path):
    agent_id, other_id = uuid.uuid4(), uuid.uuid4()
    root = tmp_path / "storage"
    docs = root / str(agent_id) / "workspace"
    (docs / "real-dir").mkdir(parents=True)
    (docs / "notes.md").write_text("inside\n", encoding="utf-8")
    (root / str(other_id)).mkdir()
    (root / str(other_id) / "secret.md").write_text("outside\n", encoding="utf-8")
    (docs / "leak-file.md").symlink_to(root / str(other_id) / "secret.md")
    (docs / "leak-dir").symlink_to(root / str(other_id), target_is_directory=True)
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: LocalStorageBackend(str(root)))

    listing = await agent_tools._storage_list_dir(agent_id, "workspace")

    assert listing == "📂 workspace: 1 folder(s), 1 file(s)\n  📁 real-dir/ (0 items)\n  📄 notes.md (7B)"


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


async def _registered_tool(
    name: str, agent_id: uuid.UUID, arguments: ToolArguments, tenant: str | None = "tenant-a"
) -> str:
    handler = resolve(name)
    assert handler is not None
    with use_execution_context(ToolExecutionContext(tenant_id=tenant, workspace_root=agent_tools.WORKSPACE_ROOT)):
        result = handler(arguments=arguments, agent_id=agent_id, user_id=agent_id, session_id=name, on_output=None)
        return result if isinstance(result, str) else await result


async def _registered_search(agent_id: uuid.UUID, arguments: ToolArguments, tenant: str | None = "tenant-a") -> str:
    return await _registered_tool("search_files", agent_id, arguments, tenant)


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


class LockRedis:
    """Just the commands the workspace lock sends, with an expiry that never runs out by itself."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiry: dict[str, int] = {}
        self.renewals = 0
        self.renewed = asyncio.Event()
        self.overloaded = False

    async def set(self, key: str, value: str, *, ex: int, nx: bool) -> bool | None:
        if nx and key in self.values:
            return None
        self.values[key], self.expiry[key] = value, ex
        return True

    async def eval(self, script: str, _count: int, key: str, token: str, *args: int) -> int:
        mine = self.values.get(key) == token
        if "'expire'" in script:
            self.renewals += 1
            self.renewed.set()
            if self.overloaded:
                raise RedisPoolOverloadError("busy")
            if mine:
                self.expiry[key] = args[0]
            self.renewed.set()
            return int(mine)
        if mine:
            del self.values[key]
        return int(mine)

    async def renewal_done(self) -> None:
        await asyncio.wait_for(self.renewed.wait(), 10)
        self.renewed.clear()


class TickClock:
    """A lock clock that only moves when the test says so: each sleep waits for a tick and advances by it."""

    def __init__(self) -> None:
        self.ticks: asyncio.Queue[float] = asyncio.Queue()
        self.time = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.time

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.time += await self.ticks.get()


@pytest.fixture
def lock_redis(monkeypatch: pytest.MonkeyPatch) -> LockRedis:
    redis = LockRedis()

    async def get_redis() -> LockRedis:
        return redis

    monkeypatch.setattr(workspace_locking, "get_redis", get_redis)
    return redis


def lock_key(agent_id: uuid.UUID, path: str) -> str:
    return f"workspace-lock:{agent_id}:{path}"


async def test_a_workspace_lock_renews_while_its_body_runs_and_stops_when_it_ends(lock_redis: LockRedis):
    agent_id, clock = uuid.uuid4(), TickClock()
    key = lock_key(agent_id, "workspace/dir")

    async with real_workspace_locks(agent_id, ["workspace/dir"], ttl_seconds=60, clock=clock):
        for _ in range(2):
            lock_redis.expiry[key] = 5
            await clock.ticks.put(20)
            await lock_redis.renewal_done()
            assert lock_redis.expiry[key] == 60
        check_workspace_lock()
    await clock.ticks.put(20)
    await asyncio.sleep(0)

    assert (lock_redis.renewals, clock.sleeps[:2], key in lock_redis.values) == (2, [20.0, 20.0], False)


async def test_a_renewal_that_finds_another_owner_marks_the_lock_lost_and_keeps_the_new_owners_lock(
    lock_redis: LockRedis,
):
    agent_id, clock = uuid.uuid4(), TickClock()
    key = lock_key(agent_id, "workspace/dir")

    async with real_workspace_locks(agent_id, ["workspace/dir"], clock=clock):
        check_workspace_lock()
        lock_redis.values[key] = "someone else"
        await clock.ticks.put(20)
        await lock_redis.renewal_done()
        with pytest.raises(WorkspaceLockLostError):
            check_workspace_lock()

    assert lock_redis.values[key] == "someone else"
    check_workspace_lock()


async def test_a_busy_redis_pool_is_survived_until_a_whole_ttl_passes_without_a_renewal(lock_redis: LockRedis):
    agent_id, clock = uuid.uuid4(), TickClock()

    async with real_workspace_locks(agent_id, ["workspace/dir"], ttl_seconds=60, clock=clock):
        lock_redis.overloaded = True
        await clock.ticks.put(20)
        await lock_redis.renewal_done()
        check_workspace_lock()
        lock_redis.overloaded = False
        await clock.ticks.put(20)
        await lock_redis.renewal_done()
        check_workspace_lock()
        lock_redis.overloaded = True
        for _ in range(2):
            await clock.ticks.put(30)
            await lock_redis.renewal_done()
        with pytest.raises(WorkspaceLockLostError):
            check_workspace_lock()


async def test_a_held_workspace_path_is_still_reported_as_busy(lock_redis: LockRedis):
    agent_id = uuid.uuid4()
    lock_redis.values[lock_key(agent_id, "workspace/dir")] = "someone else"

    with pytest.raises(RuntimeError, match="Workspace lock busy: workspace/dir"):
        async with real_workspace_locks(agent_id, ["workspace/dir"]):
            pytest.fail("the body must not run without the lock")


class EditingStorage(MemoryStorageBackend):
    """Edits one file after the folder was listed, just before the chosen batch call is sent."""

    def __init__(self, files: dict[str, bytes], edited: str, before: str):
        super().__init__(files)
        self.edited, self.before, self.calls = edited, before, 0

    async def _edit(self, call: str) -> None:
        if call == self.before and not self.calls:
            await self.write_bytes(self.edited, b"edited by someone else")
        self.calls += call == self.before

    @override
    async def delete_batch_if_match(self, versions: Sequence[StorageVersion]) -> dict[str, ConditionalWriteResult]:
        await self._edit("delete")
        return await super().delete_batch_if_match(versions)

    @override
    async def copy_batch_if_match(self, copies: Sequence[CopyRequest]) -> dict[str, ConditionalWriteResult]:
        await self._edit("copy")
        return await super().copy_batch_if_match(copies)


@pytest.fixture
def revisions(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    written: list[tuple[str, str]] = []

    async def record(*, path: str, operation: str, **_rest: object) -> None:
        written.append((path, operation))

    monkeypatch.setattr(workspace_collaboration, "record_revision", record)
    return written


async def test_a_folder_delete_leaves_a_file_edited_after_the_listing_and_names_it(
    monkeypatch, unused_db_session, revisions
):
    agent_id = uuid.uuid4()
    folder = f"{agent_id}/workspace/dir"
    storage = EditingStorage(
        {f"{folder}/a.txt": b"a", f"{folder}/b.txt": b"b", f"{folder}/sub/c.txt": b"c"}, f"{folder}/b.txt", "delete"
    )
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    result = await workspace_collaboration.delete_workspace_file(
        agent_id=agent_id, path="workspace/dir", actor_type="user", actor_id=agent_id, enforce_human_lock=False
    )

    assert (result.ok, result.rejected_paths) == (False, ["workspace/dir/b.txt"])
    assert "workspace/dir/b.txt" in result.message
    assert storage.files == {f"{folder}/b.txt": b"edited by someone else"}
    assert sorted(revisions) == [("workspace/dir/a.txt", "delete"), ("workspace/dir/sub/c.txt", "delete")]


@pytest.mark.parametrize("before", ["copy", "delete"])
async def test_a_folder_move_keeps_a_file_edited_after_the_listing_at_its_source(
    monkeypatch, unused_db_session, revisions, before
):
    agent_id = uuid.uuid4()
    source = f"{agent_id}/workspace/src"
    storage = EditingStorage(
        {f"{source}/a.txt": b"a", f"{source}/b.txt": b"b", f"{source}/sub/c.txt": b"c"}, f"{source}/b.txt", before
    )
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    result = await workspace_collaboration.move_workspace_path(
        agent_id=agent_id,
        source_path="workspace/src",
        destination_path="workspace/dst",
        actor_type="user",
        actor_id=agent_id,
        enforce_human_lock=False,
    )

    assert (result.ok, result.rejected_paths) == (False, ["workspace/src/b.txt"])
    assert storage.files[f"{source}/b.txt"] == b"edited by someone else"
    assert {key for key in storage.files if key.startswith(source)} == {f"{source}/b.txt"}
    moved = {key.removeprefix(f"{agent_id}/") for key in storage.files if "/dst/" in key} - {"workspace/dst/b.txt"}
    assert moved == {"workspace/dst/a.txt", "workspace/dst/sub/c.txt"}
    assert sorted(revisions) == [
        ("workspace/dst/a.txt", "move_destination"),
        ("workspace/dst/sub/c.txt", "move_destination"),
        ("workspace/src/a.txt", "move_source"),
        ("workspace/src/sub/c.txt", "move_source"),
    ]


async def test_a_folder_delete_stops_when_a_renewal_finds_the_lock_taken(
    monkeypatch, unused_db_session, revisions, lock_redis: LockRedis
):
    agent_id, clock = uuid.uuid4(), TickClock()
    folder = f"{agent_id}/workspace/dir"

    class StealsTheLock(MemoryStorageBackend):
        @override
        async def delete_batch_if_match(self, versions: Sequence[StorageVersion]) -> dict[str, ConditionalWriteResult]:
            results = await super().delete_batch_if_match(versions)
            lock_redis.values[lock_key(agent_id, "workspace/dir")] = "someone else"
            await clock.ticks.put(20)
            await lock_redis.renewal_done()
            return results

    storage = StealsTheLock({f"{folder}/f{index:04d}.txt": b"x" for index in range(1500)})
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(workspace_collaboration, "workspace_locks", partial(real_workspace_locks, clock=clock))

    result = await workspace_collaboration.delete_workspace_file(
        agent_id=agent_id, path="workspace/dir", actor_type="user", actor_id=agent_id, enforce_human_lock=False
    )

    assert result.ok is False
    assert "lock lost" in result.message
    assert len(storage.files) == 500
    assert len(revisions) == 1000


@pytest.fixture
def captured_revisions(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    captured: list[dict[str, object]] = []

    async def record(**kwargs: object) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(workspace_collaboration, "record_revision", record)
    return captured


REVISION_LIMIT = workspace_collaboration.MAX_REVISION_TEXT_BYTES
# (file name, size, text kept): at the limit the text is stored, one byte over or for a binary type it is left out.
REVISION_CASES = [
    ("log.txt", REVISION_LIMIT, True),
    ("log.txt", REVISION_LIMIT + 1, False),
    ("image.png", 8, False),
]


@pytest.mark.parametrize(("name", "size", "kept"), REVISION_CASES)
async def test_a_write_captures_the_previous_text_only_within_the_revision_limit(
    monkeypatch, captured_revisions, name, size, kept
):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({f"{agent_id}/workspace/{name}": b"x" * size})
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    result = await workspace_collaboration.write_workspace_file(
        agent_id=agent_id,
        path=f"workspace/{name}",
        content="new",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
    )

    assert result.ok is True
    [revision] = captured_revisions
    assert (revision["before_content"], revision["content_omitted"]) == ("x" * size if kept else None, not kept)
    assert revision["after_content"] == "new"


@pytest.mark.parametrize(("name", "size", "kept"), REVISION_CASES)
async def test_a_delete_captures_the_previous_text_only_within_the_revision_limit(
    monkeypatch, captured_revisions, name, size, kept
):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({f"{agent_id}/workspace/{name}": b"x" * size})
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    result = await workspace_collaboration.delete_workspace_file(
        agent_id=agent_id,
        path=f"workspace/{name}",
        actor_type="user",
        actor_id=agent_id,
        enforce_human_lock=False,
    )

    assert result.ok is True
    [revision] = captured_revisions
    assert (revision["before_content"], revision["content_omitted"]) == ("x" * size if kept else None, not kept)


@pytest.mark.parametrize(("name", "size", "kept"), REVISION_CASES)
async def test_a_move_captures_source_and_destination_text_only_within_the_revision_limit(
    monkeypatch, captured_revisions, name, size, kept
):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({f"{agent_id}/workspace/{name}": b"x" * size})
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    result = await workspace_collaboration.move_workspace_path(
        agent_id=agent_id,
        source_path=f"workspace/{name}",
        destination_path=f"workspace/moved-{name}",
        actor_type="user",
        actor_id=agent_id,
        enforce_human_lock=False,
    )

    assert result.ok is True
    source, destination = captured_revisions
    assert (source["operation"], source["before_content"], source["content_omitted"]) == (
        "move_source",
        "x" * size if kept else None,
        not kept,
    )
    assert (destination["operation"], destination["after_content"], destination["content_omitted"]) == (
        "move_destination",
        "x" * size if kept else None,
        not kept,
    )


async def test_a_new_file_and_a_missing_destination_are_not_marked_omitted(monkeypatch, captured_revisions):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({f"{agent_id}/workspace/a.txt": b"small"})
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    await workspace_collaboration.write_workspace_file(
        agent_id=agent_id,
        path="workspace/new.txt",
        content="fresh",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
    )
    await workspace_collaboration.move_workspace_path(
        agent_id=agent_id,
        source_path="workspace/a.txt",
        destination_path="workspace/b.txt",
        actor_type="user",
        actor_id=agent_id,
        enforce_human_lock=False,
    )

    assert [(rev["operation"], rev["content_omitted"]) for rev in captured_revisions] == [
        ("write", False),
        ("move_source", False),
        ("move_destination", False),
    ]
    assert captured_revisions[0]["before_content"] is None


# read_file and edit_file read through bounded ranges and never take a whole object into memory.
NOTES = "workspace/notes.md"
CHUNK = 64


class _RecordedReads(MemoryStorageBackend):
    """Records every bounded read as (max_bytes, offset, bytes returned) and refuses whole-object reads."""

    def __init__(self):
        super().__init__()
        self.reads: list[tuple[int, int | None, int]] = []

    @override
    async def read_bytes(self, key: str) -> bytes:
        raise AssertionError("a tool must never read a whole object")

    @override
    async def read_text(self, key: str, encoding: str = "utf-8", errors: str = "replace") -> str:
        raise AssertionError("a tool must never read a whole object")

    @override
    async def read_bytes_bounded(self, key: str, max_bytes: int, *, offset: int | None = None) -> bytes:
        data = await super().read_bytes_bounded(key, max_bytes, offset=offset)
        self.reads.append((max_bytes, offset, len(data)))
        return data


async def _registered_read(agent_id: uuid.UUID, **arguments: ToolArgumentValue) -> str:
    return await _registered_tool("read_file", agent_id, {"path": NOTES, **arguments})


async def _stored_notes(search_backend: tuple[uuid.UUID, StorageBackend], data: bytes) -> uuid.UUID:
    agent_id, backend = search_backend
    await backend.write_bytes(f"{agent_id}/{NOTES}", data)
    return agent_id


def _row_texts(result: str) -> list[str]:
    """The text of each numbered row of a read_file result, without its header and footer."""
    body = result.split("\n", 1)[1].split("\n\n... [")[0]
    return [row.partition("\t")[2] for row in body.split("\n")] if body else []


SMALL = b"one\ntwo\nthree\nfour\nfive\n"
SMALL_ROWS = "     1\tone\n     2\ttwo\n     3\tthree\n     4\tfour\n     5\tfive"


@pytest.mark.parametrize(
    ("data", "arguments", "expected"),
    [
        (SMALL, {}, f"📄 {NOTES} (lines 1-5 of 5)\n{SMALL_ROWS}"),
        (
            SMALL,
            {"offset": 1, "limit": 2},
            f"📄 {NOTES} (lines 2-3 of 5)\n     2\ttwo\n     3\tthree\n\n... [2 more lines not shown, lines 4-5]",
        ),
        (SMALL, {"offset": 4, "limit": 10}, f"📄 {NOTES} (lines 5-5 of 5)\n     5\tfive"),
        (SMALL, {"offset": 5}, "Offset 5 exceeds file length (5 lines total)"),
        (SMALL, {"offset": 90, "limit": 3}, "Offset 90 exceeds file length (5 lines total)"),
        (
            SMALL,
            {"offset": -3, "limit": 1},
            f"📄 {NOTES} (lines 1-1 of 5)\n     1\tone\n\n... [4 more lines not shown, lines 2-5]",
        ),
        (b"", {}, f"📄 {NOTES} (lines 0-0 of 0)\n"),
        (b"", {"offset": 7}, f"📄 {NOTES} (lines 0-0 of 0)\n"),
        (b"no newline", {}, f"📄 {NOTES} (lines 1-1 of 1)\n     1\tno newline"),
        (
            b"a\r\nb\rc\x0cd\xe2\x80\xa8e\xff",
            {},
            f"📄 {NOTES} (lines 1-5 of 5)\n     1\ta\n     2\tb\n     3\tc\n     4\td\n     5\te\ufffd",
        ),
    ],
)
async def test_registered_read_file_keeps_the_output_of_a_whole_read(search_backend, data, arguments, expected):
    agent_id = await _stored_notes(search_backend, data)

    assert await _registered_read(agent_id, **arguments) == expected


@pytest.mark.parametrize("boundary", ["utf8", "crlf", "lone_cr", "unicode", "invalid_utf8", "eof"])
async def test_registered_read_file_chunk_boundaries_keep_decode_and_line_semantics(
    search_backend: tuple[uuid.UUID, StorageBackend], monkeypatch: pytest.MonkeyPatch, boundary: str
):
    monkeypatch.setattr(workspace_read, "READ_CHUNK_BYTES", CHUNK)
    monkeypatch.setattr(workspace_read, "MAX_READ_CHUNK_BYTES", CHUNK)
    prefix = b"x" * (CHUNK - 1)
    data, expected = {
        "utf8": (prefix + "中needle\nlast".encode(), [prefix.decode() + "中needle", "last"]),
        "crlf": (prefix + b"\r\nneedle", [prefix.decode(), "needle"]),
        "lone_cr": (prefix + b"\rneedle", [prefix.decode(), "needle"]),
        "unicode": (prefix + "\u2028needle".encode(), [prefix.decode(), "needle"]),
        "invalid_utf8": (prefix + b"\xe4\xffneedle\nneedle", [prefix.decode() + "\ufffd\ufffdneedle", "needle"]),
        "eof": (prefix + b"x", [prefix.decode() + "x"]),
    }[boundary]
    agent_id = await _stored_notes(search_backend, data)

    result = await _registered_read(agent_id)

    assert result.split("\n", 1)[0] == f"📄 {NOTES} (lines 1-{len(expected)} of {len(expected)})"
    assert _row_texts(result) == expected


MESSY = "é中😀\r\nplain\x85two\u2028\r\n\r\n\x0b\x1c\x1d\x1e\x0cend".encode() + b"\xff\xe4\xb8"


@pytest.mark.parametrize("chunk", range(1, 14))
async def test_registered_read_file_does_not_depend_on_the_chunk_size(
    search_backend: tuple[uuid.UUID, StorageBackend], monkeypatch: pytest.MonkeyPatch, chunk: int
):
    monkeypatch.setattr(workspace_read, "READ_CHUNK_BYTES", chunk)
    monkeypatch.setattr(workspace_read, "MAX_READ_CHUNK_BYTES", 4 * chunk)
    agent_id = await _stored_notes(search_backend, MESSY)
    lines = MESSY.decode(errors="replace").splitlines()

    result = await _registered_read(agent_id)

    assert result.split("\n", 1)[0] == f"📄 {NOTES} (lines 1-{len(lines)} of {len(lines)})"
    assert _row_texts(result) == lines


async def test_registered_read_file_stops_reading_once_a_later_line_exists_and_pages_on(
    search_backend: tuple[uuid.UUID, StorageBackend], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(workspace_read, "READ_CHUNK_BYTES", CHUNK)
    monkeypatch.setattr(workspace_read, "MAX_READ_CHUNK_BYTES", CHUNK)
    lines = [f"line {number:03}" for number in range(1, 101)]
    agent_id = await _stored_notes(search_backend, "".join(f"{line}\n" for line in lines).encode())
    _, backend = search_backend
    original = backend.read_bytes_bounded
    offsets: list[int | None] = []

    async def record_range(key: str, max_bytes: int, *, offset: int | None = None) -> bytes:
        offsets.append(offset)
        return await original(key, max_bytes, offset=offset)

    monkeypatch.setattr(backend, "read_bytes_bounded", record_range)

    first = await _registered_read(agent_id, limit=2)

    assert first == (
        f"📄 {NOTES} (lines 1-2; more lines follow)\n     1\tline 001\n     2\tline 002"
        "\n\n... [more lines not shown; continue with offset=2]"
    )
    assert offsets == [0]
    collected: list[str] = []
    offset = 0
    pages = 0
    while True:
        page = await _registered_read(agent_id, offset=offset, limit=7)
        collected.extend(_row_texts(page))
        pages += 1
        continuation = re.search(r"continue with offset=(\d+)\]$", page)
        if continuation is None:
            break
        offset = int(continuation.group(1))
    assert collected == lines
    assert pages == 15
    assert page.split("\n", 1)[0] == f"📄 {NOTES} (lines 99-100 of 100)"


async def test_registered_read_file_reaches_a_deep_offset_and_a_past_end_offset_in_bounded_ranges(
    search_backend: tuple[uuid.UUID, StorageBackend], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(workspace_read, "READ_CHUNK_BYTES", CHUNK)
    monkeypatch.setattr(workspace_read, "MAX_READ_CHUNK_BYTES", CHUNK)
    lines = [f"line {number:03}" for number in range(1, 101)]
    data = "".join(f"{line}\n" for line in lines).encode()
    agent_id = await _stored_notes(search_backend, data)
    _, backend = search_backend
    original = backend.read_bytes_bounded
    ranges: list[tuple[int, int | None, int]] = []

    async def record_range(key: str, max_bytes: int, *, offset: int | None = None) -> bytes:
        chunk = await original(key, max_bytes, offset=offset)
        ranges.append((max_bytes, offset, len(chunk)))
        return chunk

    monkeypatch.setattr(backend, "read_bytes_bounded", record_range)

    deep = await _registered_read(agent_id, offset=90, limit=3)
    assert deep == (
        f"📄 {NOTES} (lines 91-93; more lines follow)\n    91\tline 091\n    92\tline 092\n    93\tline 093"
        "\n\n... [more lines not shown; continue with offset=93]"
    )
    assert [offset for _, offset, _ in ranges] == list(range(0, 833, CHUNK))

    ranges.clear()
    past_end = await _registered_read(agent_id, offset=500)
    assert past_end == "Offset 500 exceeds file length (100 lines total)"
    assert [offset for _, offset, _ in ranges] == list(range(0, len(data) + 1, CHUNK))
    assert {max_bytes for max_bytes, _, _ in ranges} == {CHUNK}
    assert max(size for _, _, size in ranges) == CHUNK
    assert ranges[-1][2] < CHUNK


async def test_registered_read_file_truncates_a_long_line_across_chunks_and_still_finds_the_next(
    search_backend: tuple[uuid.UUID, StorageBackend], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(workspace_read, "READ_CHUNK_BYTES", 4)
    monkeypatch.setattr(workspace_read, "MAX_READ_CHUNK_BYTES", 4)
    monkeypatch.setattr(workspace_read_window, "MAX_LINE_CHARS", 10)
    agent_id = await _stored_notes(search_backend, ("0123456789" + "abcdefghijklmnopqrstuvwxyz\r\nshort\n").encode())

    result = await _registered_read(agent_id)

    assert result == (
        f"📄 {NOTES} (lines 1-2 of 2)\n     1\t0123456789... [line truncated: 26 more characters]\n     2\tshort"
    )
    assert await _registered_read(agent_id, offset=1) == f"📄 {NOTES} (lines 2-2 of 2)\n     2\tshort"


@pytest.mark.parametrize(
    ("budget", "kept"),
    [(30, 2), (15, 1), (5, 1)],
    ids=["two_rows_fit", "one_row_fits", "first_row_always_kept"],
)
@pytest.mark.parametrize("chunk", [workspace_read.READ_CHUNK_BYTES, 16], ids=["whole_file_in_chunk", "early_stop"])
async def test_registered_read_file_closes_the_window_at_the_output_budget(
    search_backend: tuple[uuid.UUID, StorageBackend],
    monkeypatch: pytest.MonkeyPatch,
    budget: int,
    kept: int,
    chunk: int,
):
    monkeypatch.setattr(workspace_read, "READ_CHUNK_BYTES", chunk)
    monkeypatch.setattr(workspace_read, "MAX_READ_CHUNK_BYTES", chunk)
    monkeypatch.setattr(workspace_read_window, "MAX_WINDOW_CHARS", budget)
    agent_id = await _stored_notes(search_backend, b"".join(f"line {number:02}\n".encode() for number in range(1, 11)))

    result = await _registered_read(agent_id, limit=1_000_000)

    rows = "\n".join(f"{number:6}\tline {number:02}" for number in range(1, kept + 1))
    if chunk == 16:
        expected = f"📄 {NOTES} (lines 1-{kept}; more lines follow)\n{rows}\n\n... [more lines not shown; continue with offset={kept}]"
    else:
        expected = (
            f"📄 {NOTES} (lines 1-{kept} of 10)\n{rows}\n\n... [{10 - kept} more lines not shown, lines {kept + 1}-10]"
        )
    assert result == expected


async def test_registered_read_file_issues_only_chunk_sized_ranged_reads_on_a_large_single_line(monkeypatch):
    monkeypatch.setattr(workspace_read, "READ_CHUNK_BYTES", 1024)
    monkeypatch.setattr(workspace_read, "MAX_READ_CHUNK_BYTES", 1024)
    agent_id = uuid.uuid4()
    backend = _RecordedReads()
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: backend)
    await backend.write_bytes(f"{agent_id}/{NOTES}", b"x" * 40_000 + b"\nsecond\n")

    result = await _registered_read(agent_id)

    omitted = 40_000 - workspace_read_window.MAX_LINE_CHARS
    assert result == (
        f"📄 {NOTES} (lines 1-2 of 2)\n     1\t{'x' * workspace_read_window.MAX_LINE_CHARS}"
        f"... [line truncated: {omitted} more characters]\n     2\tsecond"
    )
    assert [offset for _, offset, _ in backend.reads] == list(range(0, 40_008, 1024))
    assert {max_bytes for max_bytes, _, _ in backend.reads} == {1024}
    assert max(size for _, _, size in backend.reads) == 1024
    assert backend.reads[-1][2] == 40_008 % 1024


@pytest.mark.parametrize(
    ("arguments", "reads", "expected"),
    [
        (
            {"offset": 3, "limit": 1},
            [(4, 0, 4), (8, 4, 8), (16, 12, 16), (16, 28, 16)],
            (
                f"📄 {NOTES} (lines 4-4; more lines follow)\n     4\tline 04"
                "\n\n... [more lines not shown; continue with offset=4]"
            ),
        ),
        (
            {},
            [(4, 0, 4), (8, 4, 8), (16, 12, 16), (16, 28, 16), (16, 44, 16), (16, 60, 12)],
            f"📄 {NOTES} (lines 1-9 of 9)\n" + "\n".join(f"{number:6}\tline {number:02}" for number in range(1, 10)),
        ),
    ],
    ids=["early_stop", "read_to_eof"],
)
async def test_registered_read_file_doubles_the_read_size_up_to_the_cap(monkeypatch, arguments, reads, expected):
    monkeypatch.setattr(workspace_read, "READ_CHUNK_BYTES", 4)
    monkeypatch.setattr(workspace_read, "MAX_READ_CHUNK_BYTES", 16)
    agent_id = uuid.uuid4()
    backend = _RecordedReads()
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: backend)
    await backend.write_bytes(
        f"{agent_id}/{NOTES}", b"".join(f"line {number:02}\n".encode() for number in range(1, 10))
    )

    result = await _registered_read(agent_id, **arguments)

    assert result == expected
    assert backend.reads == reads


EDIT_LIMIT = 64
EDIT_KEY = "workspace/a.md"


def _use_edit_storage(monkeypatch: pytest.MonkeyPatch, backend: StorageBackend) -> None:
    async def no_lock(**_arguments: object) -> None:
        return None

    monkeypatch.setattr(workspace, "get_storage_backend", lambda: backend)
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: backend)
    monkeypatch.setattr(workspace_collaboration, "get_active_lock", no_lock)
    monkeypatch.setattr(get_settings(), "WORKSPACE_FILE_CONTENT_MAX_BYTES", EDIT_LIMIT)


@pytest.fixture(params=["local", "object"])
def edit_backend(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    captured_revisions: list[dict[str, object]],
) -> tuple[uuid.UUID, StorageBackend]:
    backend = LocalStorageBackend(str(tmp_path)) if request.param == "local" else _RecordedReads()
    _use_edit_storage(monkeypatch, backend)
    return uuid.uuid4(), backend


async def _registered_edit(agent_id: uuid.UUID, old: str, new: str, **arguments: ToolArgumentValue) -> str:
    return await _registered_tool(
        "edit_file", agent_id, {"path": EDIT_KEY, "old_string": old, "new_string": new, **arguments}
    )


@pytest.mark.parametrize("size", [EDIT_LIMIT - 1, EDIT_LIMIT, EDIT_LIMIT + 1])
async def test_registered_edit_file_refuses_a_file_over_the_workspace_content_limit(edit_backend, size):
    agent_id, backend = edit_backend
    key = f"{agent_id}/{EDIT_KEY}"
    original = b"old" + b"x" * (size - 3)
    await backend.write_bytes(key, original)

    result = await _registered_edit(agent_id, "old", "new")

    if size <= EDIT_LIMIT:
        assert result == f"✅ Replaced 1 occurrence(s) in {EDIT_KEY}"
        assert await backend.read_bytes_bounded(key, EDIT_LIMIT) == b"new" + b"x" * (size - 3)
    else:
        assert result.startswith(f"❌ {EDIT_KEY} is larger than the {EDIT_LIMIT}-byte limit")
        assert "write_file" in result
        assert await backend.read_bytes_bounded(key, EDIT_LIMIT + 1) == original


@pytest.mark.parametrize(
    "original",
    [b"old \xff tail", b"\xffold", b"old \xe4\xb8", b"caf\xe9 old"],
    ids=["byte_inside", "leading_byte", "truncated_character_at_end", "latin1"],
)
async def test_registered_edit_file_refuses_invalid_utf8_and_leaves_the_file_untouched(
    edit_backend, captured_revisions, original
):
    agent_id, backend = edit_backend
    key = f"{agent_id}/{EDIT_KEY}"
    await backend.write_bytes(key, original)

    result = await _registered_edit(agent_id, "old", "new")

    assert result.startswith(f"❌ {EDIT_KEY} is not valid UTF-8")
    assert "write_file" in result
    assert await backend.read_bytes_bounded(key, EDIT_LIMIT) == original
    assert captured_revisions == []


async def test_registered_edit_file_keeps_valid_text_exactly_as_it_was(edit_backend, captured_revisions):
    agent_id, backend = edit_backend
    key = f"{agent_id}/{EDIT_KEY}"
    await backend.write_bytes(key, "\ufeffa\r\nb é\r\nb é\r\nlast".encode())

    result = await _registered_edit(agent_id, "b é", "c", replace_all=True)

    assert result == f"✅ Replaced 2 occurrence(s) in {EDIT_KEY}"
    assert await backend.read_bytes_bounded(key, EDIT_LIMIT) == "\ufeffa\r\nc\r\nc\r\nlast".encode()
    assert [(revision["operation"], revision["after_content"]) for revision in captured_revisions] == [
        ("edit", "\ufeffa\r\nc\r\nc\r\nlast")
    ]


@pytest.mark.parametrize(
    ("old", "expected_start"),
    [
        ("absent", f"❌ 'old_string' not found in {EDIT_KEY}."),
        ("twice", f"❌ 'old_string' appears 2 times in {EDIT_KEY}."),
    ],
)
async def test_registered_edit_file_keeps_its_not_found_and_ambiguous_replies(edit_backend, old, expected_start):
    agent_id, backend = edit_backend
    key = f"{agent_id}/{EDIT_KEY}"
    await backend.write_bytes(key, b"twice twice")

    result = await _registered_edit(agent_id, old, "new")

    assert result.startswith(expected_start)
    assert await backend.read_bytes_bounded(key, EDIT_LIMIT) == b"twice twice"


async def test_registered_edit_file_reads_one_range_of_at_most_the_limit_and_never_a_whole_object(
    monkeypatch, captured_revisions
):
    agent_id = uuid.uuid4()
    backend = _RecordedReads()
    _use_edit_storage(monkeypatch, backend)
    key = f"{agent_id}/{EDIT_KEY}"
    await backend.write_bytes(key, b"old")

    assert await _registered_edit(agent_id, "old", "new") == f"✅ Replaced 1 occurrence(s) in {EDIT_KEY}"

    tool_read, *_revision_reads = backend.reads
    assert tool_read == (EDIT_LIMIT, None, 3)
    assert [revision["operation"] for revision in captured_revisions] == ["edit"]


WORKSPACE_SEED = {"soul.md": b"# soul", "tasks.json": b"[]", "workspace/a.md": b"keep"}


@dataclass(frozen=True, slots=True)
class AgentFiles:
    """An agent whose workspace holds WORKSPACE_SEED, and one way the engine runs a tool call for it."""

    agent_id: uuid.UUID
    backend: StorageBackend
    run: Callable[[str, ToolArguments], Awaitable[str]]

    async def stored(self) -> dict[str, bytes | None]:
        keys = {name: f"{self.agent_id}/{name}" for name in WORKSPACE_SEED}
        return {
            name: await self.backend.read_bytes_bounded(key, EDIT_LIMIT) if await self.backend.exists(key) else None
            for name, key in keys.items()
        }


@pytest.fixture(params=["execute_tool", "execute_tool_direct"])
async def agent_files(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, edit_backend, tmp_path):
    agent_id, backend = edit_backend
    for name, data in WORKSPACE_SEED.items():
        await backend.write_bytes(f"{agent_id}/{name}", data)

    async def tenant_lookup(_agent_id: uuid.UUID) -> str:
        return "tenant-a"

    async def no_agent(_agent_id: uuid.UUID) -> None:
        return None

    async def no_activity(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _agent_id: tmp_path)
    monkeypatch.setattr(agent_dao, "get", no_agent)  # the autonomy gate lets an agent it cannot load through
    monkeypatch.setattr(activity_logger, "log_activity", no_activity)

    async def run(tool_name: str, arguments: ToolArguments) -> str:
        if request.param == "execute_tool":
            return await agent_tools.execute_tool(tool_name, arguments, agent_id=agent_id, user_id=agent_id)
        return await agent_tools._execute_tool_direct(tool_name, arguments, agent_id)

    return AgentFiles(agent_id, backend, run)


@pytest.mark.parametrize(
    ("path", "protected"),
    [
        ("soul.md", "soul.md"),
        ("/soul.md", "soul.md"),
        ("./soul.md", "soul.md"),
        ("a/../soul.md", "soul.md"),
        ("soul.md/", "soul.md"),
        ("tasks.json", "tasks.json"),
        ("./tasks.json", "tasks.json"),
        ("workspace/../tasks.json", "tasks.json"),
    ],
)
async def test_agent_delete_file_refuses_soul_md_and_tasks_json_on_the_normalised_path(agent_files, path, protected):
    result = await agent_files.run("delete_file", {"path": path})

    assert result == f"❌ {protected} cannot be deleted (protected)"
    assert await agent_files.stored() == WORKSPACE_SEED


@pytest.mark.parametrize("path", ["", ".", "/", "workspace/.."])
async def test_agent_delete_file_refuses_the_workspace_root_and_keeps_every_file(agent_files, path):
    result = await agent_files.run("delete_file", {"path": path})

    assert result.startswith("❌")
    assert await agent_files.stored() == WORKSPACE_SEED


async def test_agent_delete_file_still_deletes_other_files(agent_files):
    result = await agent_files.run("delete_file", {"path": "workspace/a.md"})

    assert result == "✅ Deleted workspace/a.md"
    assert await agent_files.stored() == {**WORKSPACE_SEED, "workspace/a.md": None}


@pytest.mark.parametrize("path", ["tasks.json", "/tasks.json", "./tasks.json", "a/../tasks.json"])
async def test_agent_write_and_edit_file_refuse_the_tasks_json_snapshot(agent_files, captured_revisions, path):
    written = await agent_files.run("write_file", {"path": path, "content": '[{"title": "x"}]'})
    edited = await agent_files.run("edit_file", {"path": path, "old_string": "[]", "new_string": '[{"title": "x"}]'})

    for result in (written, edited):
        assert result.startswith("❌")
        assert "tasks.json" in result
    assert await agent_files.stored() == WORKSPACE_SEED
    assert captured_revisions == []


@pytest.mark.parametrize("path", ["soul.md", "/soul.md"])
async def test_agent_write_and_edit_file_still_change_soul_md(agent_files, path):
    written = await agent_files.run("write_file", {"path": path, "content": "# new soul"})
    edited = await agent_files.run("edit_file", {"path": path, "old_string": "new", "new_string": "newer"})

    assert written == "✅ Written to soul.md (10 chars)"
    assert edited == "✅ Replaced 1 occurrence(s) in soul.md"
    assert (await agent_files.stored())["soul.md"] == b"# newer soul"


@pytest.mark.parametrize("path", ["/soul.md", "./tasks.json", "a/../soul.md"])
async def test_agent_move_file_refuses_soul_md_and_tasks_json_on_the_normalised_path(agent_files, path):
    result = await agent_files.run("move_file", {"source_path": path, "destination_path": "workspace/moved.md"})

    assert result.startswith("❌")
    assert "cannot be moved (protected)" in result
    assert await agent_files.stored() == WORKSPACE_SEED
