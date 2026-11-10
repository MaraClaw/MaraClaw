import hashlib
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest

from app.services import agent_context_cache
from app.services.agent_tool_exec import workspace_temp
from app.services.storage_runtime.base import StorageReadLimitExceededError
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.storage_runtime.s3 import S3StorageBackend
from test_storage_s3 import CountedBody


@pytest.fixture
def workspace_backend(tmp_path, monkeypatch):
    backend = LocalStorageBackend(str(tmp_path / "storage"))
    monkeypatch.setattr(workspace_temp, "get_storage_backend", lambda: backend)
    monkeypatch.setattr(workspace_temp.tempfile, "tempdir", str(tmp_path))
    factory = workspace_temp.tempfile.TemporaryDirectory
    created = []

    def retained_temp_directory(*args, **kwargs):
        temp = factory(*args, **kwargs)
        created.append(temp)
        return temp

    monkeypatch.setattr(workspace_temp.tempfile, "TemporaryDirectory", retained_temp_directory)

    @asynccontextmanager
    async def local_locks(*args, **kwargs):
        yield

    monkeypatch.setattr(workspace_temp, "workspace_locks", local_locks)
    yield backend
    assert all(not Path(temp.name).exists() for temp in created)


@pytest.mark.parametrize("action", ["unchanged", "edit", "delete", "remote_replace_edit", "remote_replace_delete"])
async def test_snapshot_manifest_drives_real_local_flush_and_changed_path_invalidation(
    workspace_backend, monkeypatch, action
):
    backend = workspace_backend
    agent = uuid.uuid4()
    key = f"{agent}/memory/memory.md"
    await backend.write_bytes(key, b"original")
    invalidated = []

    async def invalidate(agent_id, *paths):
        invalidated.append((agent_id, paths))

    monkeypatch.setattr(agent_context_cache, "invalidate_for_workspace_paths", invalidate)
    temp = await workspace_temp._prepare_temp_workspace(agent, paths=["memory"])
    try:
        entry = temp.manifest["memory/memory.md"]
        assert entry.base_hash == hashlib.sha256(b"original").hexdigest()
        assert entry.base_version_token == (await backend.get_version(key)).token
        if action.startswith("remote_replace"):
            replacement = backend.root / "replacement"
            await anyio.to_thread.run_sync(replacement.write_bytes, b"remote")
            await anyio.to_thread.run_sync(replacement.replace, backend.root / key)
        target = temp.root / "memory/memory.md"
        if action.endswith("edit"):
            target.write_bytes(b"edited")
        elif action.endswith("delete"):
            target.unlink()

        result = await workspace_temp.flush_temp_workspace(temp)
    finally:
        root = temp.root
        temp.cleanup()

    expected = {"updated": [], "deleted": [], "conflicted": [], "skipped": []}
    outcome = {
        "unchanged": "skipped",
        "edit": "updated",
        "delete": "deleted",
        "remote_replace_edit": "conflicted",
        "remote_replace_delete": "conflicted",
    }[action]
    expected[outcome] = ["memory/memory.md"]
    assert result == expected
    assert invalidated == ([(agent, ("memory/memory.md",))] if action in {"edit", "delete"} else [])
    if action == "delete":
        assert not (backend.root / key).exists()
    else:
        assert (backend.root / key).read_bytes() == {"edit": b"edited", "unchanged": b"original"}.get(action, b"remote")
    assert not root.exists()


@pytest.mark.parametrize(
    ("sizes", "admitted", "failure"),
    [
        ([8], [8], False),
        ([9], [], True),
        ([8, 2], [8, 2], False),
        ([8, 3, 2], [8, 2], False),
        ([8, 2, 0], [8, 2, 0], False),
        ([8, 2, 9], [], True),
    ],
)
async def test_snapshot_workspace_preserves_file_rejection_and_aggregate_skip(
    workspace_backend, tmp_path, monkeypatch, sizes, admitted, failure
):
    backend = workspace_backend
    agent = uuid.uuid4()
    for index, size in enumerate(sizes):
        await backend.write_bytes(f"{agent}/workspace/{index}", b"x" * size)
    monkeypatch.setattr(workspace_temp, "TOOL_MATERIALIZE_MAX_FILE_BYTES", 8)
    monkeypatch.setattr(workspace_temp, "TOOL_MATERIALIZE_MAX_TOTAL_BYTES", 10)

    if failure:
        with pytest.raises(StorageReadLimitExceededError):
            await workspace_temp._prepare_temp_workspace(agent, paths=["workspace"])
        assert list(tmp_path.glob("maraclaw-agent-*")) == []
    else:
        temp = await workspace_temp._prepare_temp_workspace(agent, paths=["workspace"])
        try:
            assert [entry.size for entry in temp.manifest.values()] == admitted
            assert sum(entry.size for entry in temp.manifest.values()) <= 10
        finally:
            temp.cleanup()


@pytest.mark.parametrize("failure", ["provider", "cancel"])
async def test_snapshot_preparation_failure_removes_temp_directory(workspace_backend, tmp_path, monkeypatch, failure):
    agent = uuid.uuid4()
    await workspace_backend.write_bytes(f"{agent}/workspace/input", b"data")
    started = anyio.Event()

    async def fail_snapshot(key, cap):
        started.set()
        if failure == "provider":
            raise OSError("provider failed")
        await anyio.sleep_forever()

    monkeypatch.setattr(workspace_backend, "read_snapshot_bounded", fail_snapshot)

    if failure == "provider":
        with pytest.raises(OSError, match="provider failed"):
            await workspace_temp._prepare_temp_workspace(agent, paths=["workspace"])
    else:

        async def prepare():
            await workspace_temp._prepare_temp_workspace(agent, paths=["workspace"])

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(prepare)
            with anyio.fail_after(5):
                await started.wait()
            tasks.cancel_scope.cancel()

    assert list(tmp_path.glob("maraclaw-agent-*")) == []


async def test_snapshot_workspace_aggregate_skip_consumes_only_remaining_cap_plus_one(tmp_path, monkeypatch):
    agent = uuid.uuid4()
    backend = S3StorageBackend(bucket="bucket")
    bodies = {f"{agent}/workspace/{index}": CountedBody(b"x" * size) for index, size in enumerate([8, 3, 2])}

    class Client:
        def list_objects_v2(self, **kwargs):
            return {"Contents": [{"Key": key} for key in bodies if key.startswith(kwargs["Prefix"])]}

        def get_object(self, **kwargs):
            return {"Body": bodies[kwargs["Key"]], "ContentLength": 0, "ETag": '"opaque"'}

        def head_object(self, **kwargs):
            raise AssertionError("preparation snapshots must not HEAD")

    monkeypatch.setattr(backend, "_client_or_raise", Client)
    monkeypatch.setattr(workspace_temp, "get_storage_backend", lambda: backend)
    monkeypatch.setattr(workspace_temp.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(workspace_temp, "TOOL_MATERIALIZE_MAX_FILE_BYTES", 8)
    monkeypatch.setattr(workspace_temp, "TOOL_MATERIALIZE_MAX_TOTAL_BYTES", 10)

    temp = await workspace_temp._prepare_temp_workspace(agent, paths=[f"workspace/{index}" for index in range(3)])
    try:
        assert {key: entry.size for key, entry in temp.manifest.items()} == {"workspace/0": 8, "workspace/2": 2}
        assert [(body.consumed, body.close_count) for body in bodies.values()] == [(8, 1), (3, 1), (2, 1)]
    finally:
        temp.cleanup()
