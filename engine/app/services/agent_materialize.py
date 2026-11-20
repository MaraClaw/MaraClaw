"""Rehydrate an agent's mounted directory from storage through contained writes, pulling the mirror first (S3)."""

import errno
from contextlib import aclosing
from pathlib import Path
from uuid import UUID

import anyio

from app.core.logging import logger
from app.services.storage import LocalStorageBackend, normalize_storage_key
from app.services.storage_runtime.base import StorageBackend
from app.services.storage_runtime.channel_files import copy_into_guest_file, ensure_guest_directory
from app.services.workspace_mirror import MirrorMode, MirrorReconcileError, durable_store, reconcile_agent
from app.services.workspace_mirror_local import MARKER
from app.services.workspace_mirror_scope import in_scope

_BLOCKED_PATH_ERRNOS = frozenset({errno.ELOOP, errno.ENOTDIR})
# A start waits on its restore, so it gets longer than a worker pass before an unfinished restore fails the start.
START_PULL_BUDGET_SECONDS = 120.0


class WorkspaceRestoreError(OSError):
    """Durable storage could not restore a workspace that has no usable local copy, so the start must not go on."""


async def pull_durable_state(agent_id: UUID, agent_dir: Path) -> None:
    """In S3 mode, pull storage's side of the mirror into the guest directory before the guest starts.

    The caller holds the exclusive per-agent setup lock, so no lock is taken here. The pass only pulls: it writes
    nothing to storage and charges no quota. Local mode, and S3 with the mirror disabled, do nothing.
    """
    store = durable_store()
    if store is None:
        return
    await anyio.Path(agent_dir).mkdir(parents=True, exist_ok=True)
    try:
        _ = await reconcile_agent(
            agent_id, agent_dir, store, mode=MirrorMode.PULL_ONLY, budget_seconds=START_PULL_BUDGET_SECONDS
        )
    except MirrorReconcileError as exc:
        logger.warning(
            "[WorkspaceMirror] pull before start stopped early agent={} had_base={}: {}", agent_id, exc.had_base, exc
        )
        if not exc.had_base:
            raise WorkspaceRestoreError("Workspace could not be restored from storage") from exc


async def materialize_agent_files(storage: StorageBackend, prefix: str, agent_dir: Path) -> bool:
    """Write the files storage holds under ``prefix`` into ``agent_dir``; False when storage holds nothing for it.

    With the mirror active only the durable store is listed, and only keys the mirror does not own are written.
    Otherwise the facade's tree is walked as it always was. Every write is contained in ``agent_dir``.
    """
    store = durable_store()
    if store is not None:
        await _materialize_durable(store, prefix, agent_dir)
        return True
    if not await storage.exists(prefix) and not await storage.is_dir(prefix):
        return False
    for entry in await storage.list_dir(prefix):
        await materialize_entry(storage, entry.key, agent_dir)
    return True


async def materialize_entry(storage: StorageBackend, storage_key: str, local_root: Path) -> None:
    rel = Path(storage_key).relative_to(Path(storage_key).parts[0]).as_posix()
    local_path = local_root / rel
    if local_path.is_symlink():
        return
    if await storage.is_dir(storage_key):
        try:
            ensure_guest_directory(local_root, rel)
        except OSError as exc:
            _refuse_blocked_path(exc, rel)
            return
        for child in await storage.list_dir(storage_key):
            await materialize_entry(storage, child.key, local_root)
        return
    if _is_storage_source(storage, storage_key, local_path):
        # Copying a file onto itself truncates it, which corrupts SQLite WAL/SHM files a guest holds open.
        return
    await _place(storage, storage_key, rel, local_root)


async def _materialize_durable(store: StorageBackend, prefix: str, agent_dir: Path) -> None:
    root = f"{prefix}/"
    async with aclosing(store.list_files_flat(root)) as entries:
        async for entry in entries:
            rel = entry.key.removeprefix(root)
            if not rel or normalize_storage_key(rel) != rel or "\0" in rel:
                logger.warning("[Materialize] skipped an unsafe storage key {!r}", entry.key)
            elif not _owned_elsewhere(rel):
                await _place(store, entry.key, rel, agent_dir)


def _owned_elsewhere(rel: str) -> bool:
    """Keys the mirror owns, its own marker, and the guest runtime's ``.openclaw/`` tree."""
    return in_scope(rel) or rel == MARKER or rel.partition("/")[0] == ".openclaw"


async def _place(storage: StorageBackend, storage_key: str, rel: str, agent_dir: Path) -> None:
    materialized = await storage.materialize(storage_key)
    if materialized is None:
        raise RuntimeError(f"Storage backend cannot materialize a local file for {storage_key}")
    try:
        await anyio.to_thread.run_sync(copy_into_guest_file, agent_dir, rel, materialized.path)
    except OSError as exc:
        _refuse_blocked_path(exc, rel)
    finally:
        materialized.close()


def _refuse_blocked_path(exc: OSError, rel: str) -> None:
    """Tolerate the refusal of a path a guest symlink or file blocks; every other failure propagates."""
    if exc.errno not in _BLOCKED_PATH_ERRNOS:
        raise exc
    logger.warning("[Materialize] left {} alone: a symlink or file blocks its path", rel)


def _is_storage_source(storage: StorageBackend, storage_key: str, local_path: Path) -> bool:
    """True when ``local_path`` is the very file local storage serves for ``storage_key``."""
    return (
        isinstance(storage, LocalStorageBackend)
        and local_path.exists()
        and (storage.root / storage_key).samefile(local_path)
    )
