"""Check file references without reading bytes or promoting fallback objects."""

from uuid import UUID

import anyio

from app.services.storage_runtime.agent_files import agent_storage_key
from app.services.storage_runtime.base import StorageBackend
from app.services.storage_runtime.fallback import FallbackStorageBackend
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.workspace_paths import WorkspacePathError, resolve_path_within_root


def _local_agent_file(storage: LocalStorageBackend, agent_id: UUID, path: str) -> bool:
    try:
        root = storage.root.resolve() / str(agent_id)
        # Even the agent directory itself must not alias another agent's storage.
        if root.resolve() != root:
            return False
        return resolve_path_within_root(root, path, allow_root=False).is_file()
    except WorkspacePathError, OSError:
        return False


async def is_agent_file(storage: StorageBackend, agent_id: UUID, path: str) -> bool:
    """The caller supplies an already parsed, canonical agent-relative path."""
    match storage:
        case LocalStorageBackend():
            return await anyio.to_thread.run_sync(_local_agent_file, storage, agent_id, path)
        case FallbackStorageBackend():
            return await is_agent_file(storage.primary, agent_id, path) or await is_agent_file(
                storage.fallback, agent_id, path
            )
        case _:
            return await storage.is_file(agent_storage_key(agent_id, path))
