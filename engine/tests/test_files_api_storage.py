import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import override

import pytest
from fastapi import HTTPException

from app.api import files
from app.services.agent_manager import AgentManager
from app.services.storage_runtime.base import StorageBackend, StorageEntry, StorageVersion


class _FakeDb:
    def close(self):
        return None


class PrefixOnlyStorage(StorageBackend):
    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects = dict(objects or {})

    @override
    async def exists(self, key: str) -> bool:
        return key in self.objects

    @override
    async def is_file(self, key: str) -> bool:
        return key in self.objects

    @override
    async def is_dir(self, key: str) -> bool:
        prefix = key.rstrip("/") + "/"
        return any(existing.startswith(prefix) for existing in self.objects)

    @override
    async def list_dir(self, key: str) -> list[StorageEntry]:
        prefix = key.rstrip("/") + "/"
        entries_by_name: dict[str, StorageEntry] = {}
        for existing, data in self.objects.items():
            if not existing.startswith(prefix):
                continue
            rest = existing.removeprefix(prefix)
            name, _, tail = rest.partition("/")
            entries_by_name[name] = StorageEntry(
                name=name,
                key=f"{prefix}{name}",
                is_dir=bool(tail),
                size=0 if tail else len(data),
            )
        return sorted(entries_by_name.values(), key=lambda entry: (not entry.is_dir, entry.name))

    @override
    async def read_bytes(self, key: str) -> bytes:
        return self.objects[key]

    @override
    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[key] = data

    @override
    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    @override
    async def delete_tree(self, key: str) -> None:
        prefix = key.rstrip("/") + "/"
        for existing in list(self.objects):
            if existing.startswith(prefix):
                self.objects.pop(existing, None)

    @override
    async def stat(self, key: str) -> StorageEntry:
        if key not in self.objects:
            raise FileNotFoundError(key)
        return StorageEntry(name=key.rsplit("/", 1)[-1], key=key, is_dir=False, size=len(self.objects[key]))

    @override
    async def get_version(self, key: str) -> StorageVersion:
        if key not in self.objects:
            return StorageVersion(key=key, exists=False, is_dir=False)
        token = f"v:{len(self.objects[key])}"
        return StorageVersion(
            key=key,
            exists=True,
            is_dir=False,
            size=len(self.objects[key]),
            version_id=token,
            etag=token,
            content_hash=token,
        )


@pytest.fixture
async def unused_db_session() -> AsyncIterator[_FakeDb]:
    session = _FakeDb()
    try:
        yield session
    finally:
        session.close()


def make_user():
    return SimpleNamespace(id=uuid.uuid4(), display_name="Storage User", role="member", tenant_id=None, is_active=True)


def make_agent(agent_id: uuid.UUID, creator_id: uuid.UUID):
    return SimpleNamespace(id=agent_id, name="Storage Agent", creator_id=creator_id, primary_model_id=None)


async def allow_access(user, agent_id: uuid.UUID, db: object = None):
    return make_agent(agent_id, user.id), "manage"


@pytest.mark.asyncio
async def test_list_files_hides_focus_file_from_agent_root(monkeypatch, unused_db_session: object):
    agent_id = uuid.uuid4()
    storage = PrefixOnlyStorage({f"{agent_id}/focus.md": b"# Focus\n"})
    monkeypatch.setattr(files, "get_storage_backend", lambda: storage)

    monkeypatch.setattr(files, "check_agent_access", allow_access)
    user = make_user()

    result = await files.list_files(agent_id, path="", current_user=user)

    assert result == []


@pytest.mark.asyncio
async def test_list_files_allows_empty_agent_root(monkeypatch, unused_db_session: object):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(files, "get_storage_backend", lambda: PrefixOnlyStorage())

    monkeypatch.setattr(files, "check_agent_access", allow_access)
    user = make_user()

    assert await files.list_files(agent_id, path="", current_user=user) == []


@pytest.mark.asyncio
async def test_read_file_rejects_database_backed_focus_file(monkeypatch, unused_db_session: object):
    agent_id = uuid.uuid4()
    storage = PrefixOnlyStorage({f"{agent_id}/focus.md": b"# Focus\n"})
    monkeypatch.setattr(files, "get_storage_backend", lambda: storage)

    monkeypatch.setattr(files, "check_agent_access", allow_access)
    user = make_user()

    with pytest.raises(HTTPException) as exc:
        await files.read_file(agent_id, path="focus.md", current_user=user)

    assert exc.value.status_code == 410
    assert exc.value.detail == "Focus is stored in the system database. Use the Focus API."


@pytest.mark.asyncio
async def test_agent_manager_does_not_reinitialize_s3_prefix_directory(
    monkeypatch, tmp_path, unused_db_session: object
):
    agent_id = uuid.uuid4()
    storage = PrefixOnlyStorage({f"{agent_id}/soul.md": b"existing"})
    monkeypatch.setattr("app.services.agent_manager.get_storage_backend", lambda: storage)
    monkeypatch.setattr("app.services.agent_manager.settings.STORAGE_LOCAL_ROOT", str(tmp_path))

    manager = AgentManager()
    agent = make_agent(agent_id, uuid.uuid4())

    await manager.initialize_agent_files(agent=agent)

    assert storage.objects[f"{agent_id}/soul.md"] == b"existing"


@pytest.mark.asyncio
async def test_agent_manager_materializes_s3_prefix_directory(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = PrefixOnlyStorage(
        {
            f"{agent_id}/soul.md": b"# Soul\n",
            f"{agent_id}/memory/memory.md": b"# Memory\n",
        }
    )
    monkeypatch.setattr("app.services.agent_manager.get_storage_backend", lambda: storage)
    monkeypatch.setattr("app.services.agent_manager.settings.STORAGE_LOCAL_ROOT", str(tmp_path))

    manager = AgentManager()

    agent_dir = await manager._materialize_agent_dir(agent_id)

    assert (agent_dir / "soul.md").read_text(encoding="utf-8") == "# Soul\n"
    assert (agent_dir / "memory" / "memory.md").read_text(encoding="utf-8") == "# Memory\n"
