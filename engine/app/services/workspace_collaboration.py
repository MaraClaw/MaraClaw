"""Workspace collaboration helpers.

All user and agent writes should pass through this module so file history,
rollback, and human edit locks remain consistent across REST APIs and tools.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiofiles

from app.dao.workspace_dao import workspace_edit_lock_dao, workspace_file_revision_dao
from app.records.workspace import WorkspaceEditLockRecord, WorkspaceFileRevisionRecord
from app.services.storage import get_storage_backend, normalize_storage_key
from app.services.storage_runtime.base import StorageBackend, WriteCondition
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.workspace_locking import workspace_locks

USER_AUTOSAVE_MERGE_SECONDS = 60
EDIT_LOCK_TTL_SECONDS = 90
MAX_REVISION_TEXT_BYTES = 512 * 1024
BINARY_REVISION_EXTENSIONS = {
    ".7z",
    ".avif",
    ".bin",
    ".bmp",
    ".doc",
    ".docx",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp3",
    ".mp4",
    ".odp",
    ".ods",
    ".odt",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".tar",
    ".webp",
    ".xls",
    ".xlsx",
    ".zip",
}


@dataclass
class WorkspaceWriteResult:
    ok: bool
    path: str
    message: str
    revision_id: str | None = None
    locked_by_user_id: str | None = None


def _should_mirror_to_local_filesystem(storage: StorageBackend) -> bool:
    """Only mirror writes into AGENT_DATA_DIR when the filesystem is the primary store."""
    return isinstance(storage, LocalStorageBackend)


def content_hash(content: str | None) -> str:
    """Return a stable hash for text content."""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def normalize_workspace_path(path: str) -> str:
    """Normalize a workspace path without allowing absolute traversal."""
    clean = (path or "").replace("\\", "/").strip().lstrip("/")
    parts: list[str] = []
    for part in clean.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                _ = parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def safe_agent_path(base: Path, path: str) -> Path:
    """Resolve a path under an agent directory and reject traversal."""
    rel = normalize_workspace_path(path)
    target = (base / rel).resolve()
    if not str(target).startswith(str(base.resolve())):
        raise ValueError("Path traversal not allowed")
    return target


async def read_text_if_exists(path: Path) -> str | None:
    """Read a UTF-8 text file if it exists; return None for missing/binary files."""
    if not await asyncio.to_thread(path.exists) or not await asyncio.to_thread(path.is_file):
        return None
    if path.suffix.lower() in BINARY_REVISION_EXTENSIONS:
        return None
    try:
        if (await asyncio.to_thread(path.stat)).st_size > MAX_REVISION_TEXT_BYTES:
            return None
    except OSError:
        return None
    async with aiofiles.open(path, "rb") as f:
        data = await f.read()
    if b"\x00" in data:
        return None
    return data.decode("utf-8", errors="replace")


async def cleanup_expired_locks(db: object | None = None) -> None:
    """Remove stale edit locks.

    ``db`` is accepted for call-site compatibility and ignored (psycopg path).
    """
    del db
    _ = await workspace_edit_lock_dao.delete_expired(now=datetime.now(UTC))


async def acquire_edit_lock(
    db: object | None = None,
    *,
    agent_id: uuid.UUID,
    path: str,
    user_id: uuid.UUID,
    session_id: str | None = None,
) -> WorkspaceEditLockRecord:
    """Acquire or refresh a human edit lock.

    ``db`` is accepted for call-site compatibility and ignored (psycopg path).
    """
    del db
    await cleanup_expired_locks()
    normalized = normalize_workspace_path(path)
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=EDIT_LOCK_TTL_SECONDS)
    return await workspace_edit_lock_dao.upsert_for_path(
        agent_id=agent_id,
        path=normalized,
        user_id=user_id,
        session_id=session_id,
        expires_at=expires_at,
    )


async def release_edit_lock(
    db: object | None = None,
    *,
    agent_id: uuid.UUID,
    path: str,
    user_id: uuid.UUID,
) -> None:
    """Release a human edit lock owned by a user.

    ``db`` is accepted for call-site compatibility and ignored (psycopg path).
    """
    del db
    await workspace_edit_lock_dao.delete_for_user_path(
        agent_id=agent_id,
        path=normalize_workspace_path(path),
        user_id=user_id,
    )


async def get_active_lock(
    db: object | None = None,
    *,
    agent_id: uuid.UUID,
    path: str,
) -> WorkspaceEditLockRecord | None:
    """Return an active lock for a file, if present.

    ``db`` is accepted for call-site compatibility and ignored (psycopg path).
    """
    del db
    await cleanup_expired_locks()
    return await workspace_edit_lock_dao.get_for_path(agent_id, normalize_workspace_path(path))


async def record_revision(
    db: object | None = None,
    *,
    agent_id: uuid.UUID,
    path: str,
    operation: str,
    actor_type: str,
    actor_id: uuid.UUID | None,
    before_content: str | None,
    after_content: str | None,
    session_id: str | None = None,
    merge_user_autosave: bool = False,
) -> WorkspaceFileRevisionRecord | None:
    """Record a revision, optionally merging rapid user autosaves.

    ``db`` is accepted for call-site compatibility and ignored (psycopg path).
    """
    del db
    normalized = normalize_workspace_path(path)
    # PostgreSQL text columns cannot store NUL bytes. Treat such content as
    # non-text revision data so binary files can still be moved/deleted safely.
    before_content = before_content.replace("\x00", "") if before_content is not None else None
    after_content = after_content.replace("\x00", "") if after_content is not None else None
    before = before_content or ""
    after = after_content or ""
    if before == after and operation not in {"delete", "move_source", "move_destination"}:
        return None

    group_key = None
    if merge_user_autosave and actor_type == "user" and actor_id:
        group_key = f"user-autosave:{agent_id}:{normalized}:{actor_id}"
        cutoff = datetime.now(UTC) - timedelta(seconds=USER_AUTOSAVE_MERGE_SECONDS)
        existing = await workspace_file_revision_dao.find_mergeable_autosave(
            agent_id=agent_id,
            path=normalized,
            actor_id=actor_id,
            group_key=group_key,
            cutoff=cutoff,
        )
        if existing:
            return await workspace_file_revision_dao.update(
                db_obj=existing,
                obj_in={
                    "after_content": after,
                    "content_hash": content_hash(after),
                    "session_id": session_id or existing.session_id,
                },
            )

    return await workspace_file_revision_dao.create(
        obj_in={
            "agent_id": agent_id,
            "path": normalized,
            "operation": operation,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "session_id": session_id,
            "before_content": before_content,
            "after_content": after_content,
            "content_hash": content_hash(after_content),
            "group_key": group_key,
        }
    )


async def write_workspace_file(
    db: object | None = None,
    *,
    agent_id: uuid.UUID,
    base_dir: Path,
    path: str,
    content: str,
    actor_type: str,
    actor_id: uuid.UUID | None,
    operation: str = "write",
    session_id: str | None = None,
    enforce_human_lock: bool = True,
    merge_user_autosave: bool = False,
    expected_version_token: str | None = None,
) -> WorkspaceWriteResult:
    """Write text content, enforcing human locks for agent/system actors.

    ``db`` is accepted for call-site compatibility and ignored (psycopg path).
    """
    del db
    normalized = normalize_workspace_path(path)
    if not normalized:
        return WorkspaceWriteResult(False, normalized, "Missing file path")

    if enforce_human_lock and actor_type != "user":
        lock = await get_active_lock(agent_id=agent_id, path=normalized)
        if lock:
            return WorkspaceWriteResult(
                False,
                normalized,
                (
                    f"Human is currently editing {normalized}. Do not modify it now. "
                    + "Ask the user to finish editing, or work on another file."
                ),
                locked_by_user_id=str(lock.user_id),
            )

    storage = get_storage_backend()
    storage_key = normalize_storage_key(f"{agent_id}/{normalized}")
    local_base_available = _should_mirror_to_local_filesystem(storage)
    try:
        target = safe_agent_path(base_dir, normalized)
    except Exception:
        target = None
        local_base_available = False
    before = (
        await storage.read_text(storage_key, encoding="utf-8", errors="replace")
        if await storage.exists(storage_key)
        else None
    )
    write_result = await storage.write_bytes_if_match(
        storage_key,
        content.encode("utf-8"),
        condition=WriteCondition(version_token=expected_version_token) if expected_version_token is not None else None,
        content_type="text/plain; charset=utf-8",
    )
    if not write_result.ok:
        return WorkspaceWriteResult(False, normalized, f"Conflict detected while writing {normalized}")
    if local_base_available and target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(target, "w", encoding="utf-8") as f:
            _ = await f.write(content)

    revision = await record_revision(
        agent_id=agent_id,
        path=normalized,
        operation=operation,
        actor_type=actor_type,
        actor_id=actor_id,
        before_content=before,
        after_content=content,
        session_id=session_id,
        merge_user_autosave=merge_user_autosave,
    )
    from app.services.agent_context_cache import invalidate_for_workspace_paths

    await invalidate_for_workspace_paths(agent_id, normalized)
    return WorkspaceWriteResult(
        True,
        normalized,
        f"Written to {normalized} ({len(content)} chars)",
        revision_id=str(revision.id) if revision else None,
    )


async def delete_workspace_file(
    db: object | None = None,
    *,
    agent_id: uuid.UUID,
    base_dir: Path,
    path: str,
    actor_type: str,
    actor_id: uuid.UUID | None,
    session_id: str | None = None,
    enforce_human_lock: bool = True,
    expected_version_token: str | None = None,
) -> WorkspaceWriteResult:
    """Delete a workspace file and record the deleted content.

    ``db`` is accepted for call-site compatibility and ignored (psycopg path).
    """
    del db
    normalized = normalize_workspace_path(path)
    storage = get_storage_backend()
    storage_key = normalize_storage_key(f"{agent_id}/{normalized}")
    target = None
    if _should_mirror_to_local_filesystem(storage):
        try:
            target = safe_agent_path(base_dir, normalized)
        except Exception:
            target = None
    if enforce_human_lock and actor_type != "user":
        lock = await get_active_lock(agent_id=agent_id, path=normalized)
        if lock:
            return WorkspaceWriteResult(
                False,
                normalized,
                f"Human is currently editing {normalized}. Do not delete it now.",
                locked_by_user_id=str(lock.user_id),
            )
    storage_exists = await storage.exists(storage_key)
    storage_is_dir = await storage.is_dir(storage_key)
    if not storage_exists and not storage_is_dir:
        return WorkspaceWriteResult(False, normalized, f"File not found: {normalized}")
    before = (
        await storage.read_text(storage_key, encoding="utf-8", errors="replace")
        if storage_exists and await storage.is_file(storage_key)
        else None
    )
    async with workspace_locks(agent_id, [normalized]):
        if storage_is_dir:
            entries = await _collect_storage_tree_versions(storage, storage_key)
            for entry_key, version_token in reversed(entries):
                delete_result = await storage.delete_if_match(
                    entry_key,
                    condition=WriteCondition(version_token=version_token),
                )
                if not delete_result.ok:
                    return WorkspaceWriteResult(False, normalized, f"Conflict detected while deleting {normalized}")
        else:
            delete_result = await storage.delete_if_match(
                storage_key,
                condition=WriteCondition(version_token=expected_version_token)
                if expected_version_token is not None
                else None,
            )
            if not delete_result.ok:
                return WorkspaceWriteResult(False, normalized, f"Conflict detected while deleting {normalized}")
    if target is not None and target.exists():
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    revision = await record_revision(
        agent_id=agent_id,
        path=normalized,
        operation="delete",
        actor_type=actor_type,
        actor_id=actor_id,
        before_content=before,
        after_content=None,
        session_id=session_id,
    )
    from app.services.agent_context_cache import invalidate_for_workspace_paths

    await invalidate_for_workspace_paths(agent_id, normalized)
    return WorkspaceWriteResult(
        True,
        normalized,
        f"Deleted {normalized}",
        revision_id=str(revision.id) if revision else None,
    )


async def move_workspace_path(
    db: object | None = None,
    *,
    agent_id: uuid.UUID,
    base_dir: Path,
    source_path: str,
    destination_path: str,
    actor_type: str,
    actor_id: uuid.UUID | None,
    session_id: str | None = None,
    enforce_human_lock: bool = True,
    overwrite: bool = False,
    expected_source_version_token: str | None = None,
    expected_destination_version_token: str | None = None,
) -> WorkspaceWriteResult:
    """Move or rename a workspace file/folder while respecting edit locks.

    ``db`` is accepted for call-site compatibility and ignored (psycopg path).
    """
    del db
    source_normalized = normalize_workspace_path(source_path)
    destination_normalized = normalize_workspace_path(destination_path)
    if not source_normalized:
        return WorkspaceWriteResult(False, source_normalized, "Missing source path")
    if not destination_normalized:
        return WorkspaceWriteResult(False, destination_normalized, "Missing destination path")
    if source_normalized in {"tasks.json", "soul.md"}:
        return WorkspaceWriteResult(False, source_normalized, f"{source_normalized} cannot be moved (protected)")

    storage = get_storage_backend()
    source_key = normalize_storage_key(f"{agent_id}/{source_normalized}")
    source_exists = await storage.exists(source_key)
    source_is_dir = await storage.is_dir(source_key)
    if not source_exists and not source_is_dir:
        return WorkspaceWriteResult(False, source_normalized, f"File not found: {source_normalized}")

    destination_key = normalize_storage_key(f"{agent_id}/{destination_normalized}")
    destination_is_dir = await storage.is_dir(destination_key)
    if destination_path.replace("\\", "/").strip().endswith("/") or destination_is_dir:
        destination_normalized = normalize_workspace_path(f"{destination_normalized}/{Path(source_normalized).name}")
        destination_key = normalize_storage_key(f"{agent_id}/{destination_normalized}")

    if source_normalized == destination_normalized:
        return WorkspaceWriteResult(False, source_normalized, "Source and destination are the same")
    if source_is_dir and (
        destination_normalized == source_normalized or destination_normalized.startswith(source_normalized + "/")
    ):
        return WorkspaceWriteResult(False, source_normalized, "Cannot move a folder into itself")

    if enforce_human_lock and actor_type != "user":
        for locked_path in (source_normalized, destination_normalized):
            lock = await get_active_lock(agent_id=agent_id, path=locked_path)
            if lock:
                return WorkspaceWriteResult(
                    False,
                    locked_path,
                    (
                        f"Human is currently editing {locked_path}. Do not move it now. "
                        + "Ask the user to finish editing, or choose another path."
                    ),
                    locked_by_user_id=str(lock.user_id),
                )

    destination_exists = await storage.exists(destination_key)
    destination_is_dir = await storage.is_dir(destination_key)
    async with workspace_locks(agent_id, [source_normalized, destination_normalized]):
        if destination_exists or destination_is_dir:
            if not overwrite:
                return WorkspaceWriteResult(
                    False,
                    destination_normalized,
                    f"Destination already exists: {destination_normalized}. Set overwrite=true to replace it.",
                )
            if destination_is_dir:
                await storage.delete_tree(destination_key)
            else:
                delete_result = await storage.delete_if_match(
                    destination_key,
                    condition=WriteCondition(version_token=expected_destination_version_token)
                    if expected_destination_version_token is not None
                    else None,
                )
                if not delete_result.ok:
                    return WorkspaceWriteResult(
                        False, destination_normalized, f"Conflict detected while replacing {destination_normalized}"
                    )

        source = destination = None
        if _should_mirror_to_local_filesystem(storage):
            source = safe_agent_path(base_dir, source_normalized)
            destination = safe_agent_path(base_dir, destination_normalized)
        source_before = (
            await storage.read_text(source_key, encoding="utf-8", errors="replace") if source_exists else None
        )
        destination_before = (
            await storage.read_text(destination_key, encoding="utf-8", errors="replace") if destination_exists else None
        )

        if source_is_dir:
            entries = await _collect_storage_tree_versions(storage, source_key)
            for entry_key, version_token in entries:
                rel = entry_key.removeprefix(source_key.rstrip("/") + "/")
                target_key = normalize_storage_key(f"{agent_id}/{destination_normalized}/{rel}")
                current_version = await storage.get_version(entry_key)
                if current_version.token != version_token:
                    return WorkspaceWriteResult(
                        False, source_normalized, f"Conflict detected while moving {source_normalized}"
                    )
                await storage.write_bytes(target_key, await storage.read_bytes(entry_key))
            for entry_key, version_token in reversed(entries):
                delete_result = await storage.delete_if_match(
                    entry_key,
                    condition=WriteCondition(version_token=version_token),
                )
                if not delete_result.ok:
                    return WorkspaceWriteResult(
                        False, source_normalized, f"Conflict detected while finalizing move for {source_normalized}"
                    )
        else:
            source_version = await storage.get_version(source_key)
            if expected_source_version_token is not None and source_version.token != expected_source_version_token:
                return WorkspaceWriteResult(
                    False, source_normalized, f"Conflict detected while moving {source_normalized}"
                )
            await storage.write_bytes(destination_key, await storage.read_bytes(source_key))
            delete_result = await storage.delete_if_match(
                source_key,
                condition=WriteCondition(version_token=source_version.token),
            )
            if not delete_result.ok:
                return WorkspaceWriteResult(
                    False, source_normalized, f"Conflict detected while finalizing move for {source_normalized}"
                )

        destination_after = (
            await storage.read_text(destination_key, encoding="utf-8", errors="replace")
            if await storage.is_file(destination_key)
            else None
        )

        if source is not None and source.exists():
            if source.is_dir():
                shutil.rmtree(source)
            else:
                source.unlink()
        if destination is not None and await storage.is_file(destination_key):
            destination.parent.mkdir(parents=True, exist_ok=True)
            _ = destination.write_bytes(await storage.read_bytes(destination_key))

    source_revision = await record_revision(
        agent_id=agent_id,
        path=source_normalized,
        operation="move_source",
        actor_type=actor_type,
        actor_id=actor_id,
        before_content=source_before,
        after_content=None,
        session_id=session_id,
    )
    destination_revision = await record_revision(
        agent_id=agent_id,
        path=destination_normalized,
        operation="move_destination",
        actor_type=actor_type,
        actor_id=actor_id,
        before_content=destination_before,
        after_content=destination_after,
        session_id=session_id,
    )
    revision = destination_revision or source_revision
    from app.services.agent_context_cache import invalidate_for_workspace_paths

    await invalidate_for_workspace_paths(agent_id, source_normalized, destination_normalized)
    return WorkspaceWriteResult(
        True,
        destination_normalized,
        f"Moved {source_normalized} to {destination_normalized}",
        revision_id=str(revision.id) if revision else None,
    )


async def _collect_storage_tree_versions(storage: StorageBackend, root_key: str) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    for entry in await storage.list_dir(root_key):
        if entry.is_dir:
            keys.extend(await _collect_storage_tree_versions(storage, entry.key))
        else:
            version = await storage.get_version(entry.key)
            keys.append((entry.key, version.token))
    return keys


async def list_revisions(
    db: object | None = None,
    *,
    agent_id: uuid.UUID,
    path: str,
    limit: int = 50,
) -> list[WorkspaceFileRevisionRecord]:
    """List recent revisions for one file.

    ``db`` is accepted for call-site compatibility and ignored (psycopg path).
    """
    del db
    return list(
        await workspace_file_revision_dao.list_for_path(
            agent_id,
            normalize_workspace_path(path),
            limit=min(max(limit, 1), 100),
        )
    )
