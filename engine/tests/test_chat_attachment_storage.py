from dataclasses import dataclass
from typing import override
from uuid import uuid4

import pytest

from app.services.storage_runtime import FallbackStorageBackend, LocalStorageBackend, StorageBackend
from app.services.storage_runtime.agent_file_refs import is_agent_file


@dataclass(frozen=True, slots=True)
class RemoteFiles(StorageBackend):
    keys: frozenset[str]

    @override
    async def is_file(self, key: str) -> bool:
        return key in self.keys


@pytest.mark.parametrize("backend", ["remote", "fallback_remote", "fallback_local"])
async def test_file_reference_uses_authorized_agent_key_on_configured_backend(tmp_path, backend):
    # Given
    agent_id = uuid4()
    path = "workspace/uploads/source.pptx"
    local = LocalStorageBackend(str(tmp_path))
    file = tmp_path / str(agent_id) / path
    file.parent.mkdir(parents=True)
    file.write_bytes(b"not read or promoted")
    remote = RemoteFiles(frozenset({f"{agent_id}/{path}"}) if backend != "fallback_local" else frozenset())
    storage = remote if backend == "remote" else FallbackStorageBackend(remote, local)
    # When
    allowed = await is_agent_file(storage, agent_id, path)
    foreign = await is_agent_file(storage, uuid4(), path)
    # Then
    assert allowed is True
    assert foreign is False


async def test_fallback_local_symlink_cannot_alias_other_agent_storage(tmp_path):
    # Given
    agent_id = uuid4()
    target = tmp_path / "other-agent" / "private.txt"
    target.parent.mkdir()
    target.write_bytes(b"private")
    root = tmp_path / str(agent_id)
    root.mkdir()
    (root / "alias.txt").symlink_to(target)
    storage = FallbackStorageBackend(RemoteFiles(frozenset()), LocalStorageBackend(str(tmp_path)))
    # When
    allowed = await is_agent_file(storage, agent_id, "alias.txt")
    # Then
    assert allowed is False
