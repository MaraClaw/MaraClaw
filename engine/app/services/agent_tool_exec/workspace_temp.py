from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import anyio

from app.core.logging import logger
from app.services.storage import get_storage_backend, normalize_storage_key
from app.services.storage_runtime.base import (
    StorageBackend,
    StorageReadLimitExceededError,
    WriteCondition,
    content_hash_bytes,
)
from app.services.storage_runtime.bounded import read_bounded
from app.services.workspace_collaboration import normalize_workspace_path
from app.services.workspace_locking import workspace_locks

_FLUSH_CHUNK_BYTES: Final = 1024 * 1024
TOOL_MATERIALIZE_MAX_FILE_BYTES: Final = 10 * 1024 * 1024
TOOL_MATERIALIZE_MAX_TOTAL_BYTES: Final = 100 * 1024 * 1024
TEMP_WORKSPACE_DEFAULT_PATHS: Final = ["workspace", "memory", "skills", "focus.md", "soul.md", "HEARTBEAT.md"]

type FlushTempWorkspaceResult = dict[str, list[str]]


@dataclass
class TempWorkspaceManifestEntry:
    rel_path: str
    storage_key: str
    base_version_token: str
    base_hash: str
    size: int


@dataclass
class TempWorkspace:
    temp_dir: tempfile.TemporaryDirectory[str]
    root: Path
    agent_id: uuid.UUID
    tenant_id: str | None
    selected_paths: list[str]
    manifest: dict[str, TempWorkspaceManifestEntry]

    def cleanup(self) -> None:
        self.temp_dir.cleanup()


def _safe_local_target(local_root: Path, rel_path: str) -> Path | None:
    root_resolved = local_root.resolve()
    target = (local_root / rel_path).resolve()
    if not target.is_relative_to(root_resolved):
        return None
    return target


async def _materialize_storage_workspace(storage: StorageBackend, storage_key: str, local_root: Path) -> None:
    if not await storage.is_dir(storage_key):
        return
    for entry in await storage.list_dir(storage_key):
        await _materialize_storage_entry(storage, entry.key, storage_key, local_root)


async def _materialize_storage_entry(
    storage: StorageBackend,
    entry_key: str,
    root_key: str,
    local_root: Path,
) -> None:
    rel = entry_key.removeprefix(root_key.rstrip("/") + "/")
    target = _safe_local_target(local_root, rel)
    if target is None:
        return
    if await storage.is_dir(entry_key):
        target.mkdir(parents=True, exist_ok=True)
        for child in await storage.list_dir(entry_key):
            await _materialize_storage_entry(storage, child.key, root_key, local_root)
        return
    data = await storage.read_bytes_bounded(entry_key, TOOL_MATERIALIZE_MAX_FILE_BYTES)
    _ = await anyio.to_thread.run_sync(_write_local_file, target, data)


def _write_local_file(target: Path, data: bytes) -> str:
    """Write ``data`` under a new parent and return its content hash, off the event loop."""
    target.parent.mkdir(parents=True, exist_ok=True)
    _ = target.write_bytes(data)
    return content_hash_bytes(data)


async def _prepare_temp_workspace(
    agent_id: uuid.UUID,
    tenant_id: str | None = None,
    paths: list[str] | None = None,
) -> TempWorkspace:
    tmp = tempfile.TemporaryDirectory(prefix=f"maraclaw-agent-{str(agent_id)[:8]}-")
    temp_ws = Path(tmp.name)
    for folder in ("workspace", "memory", "skills"):
        (temp_ws / folder).mkdir(parents=True, exist_ok=True)

    from app.services import agent_tools

    storage = get_storage_backend()
    budget = {"total": 0}
    selected = TEMP_WORKSPACE_DEFAULT_PATHS if paths is None else [path for path in paths if path]
    manifest: dict[str, TempWorkspaceManifestEntry] = {}
    try:
        for rel_path in selected:
            storage_key, normalized, is_enterprise = agent_tools._tool_storage_key(agent_id, rel_path, tenant_id)
            if is_enterprise:
                continue
            await _materialize_storage_path_with_budget(storage, storage_key, normalized, temp_ws, budget, manifest)
    except BaseException:
        tmp.cleanup()
        raise
    return TempWorkspace(
        temp_dir=tmp,
        root=temp_ws,
        agent_id=agent_id,
        tenant_id=tenant_id,
        selected_paths=list(selected),
        manifest=manifest,
    )


async def _materialize_storage_path_with_budget(
    storage: StorageBackend,
    storage_key: str,
    rel_path: str,
    local_root: Path,
    budget: dict[str, int],
    manifest: dict[str, TempWorkspaceManifestEntry],
) -> None:
    if await storage.is_file(storage_key):
        remaining = TOOL_MATERIALIZE_MAX_TOTAL_BYTES - budget["total"]
        version = await storage.get_version_bounded(storage_key, TOOL_MATERIALIZE_MAX_FILE_BYTES)
        try:
            data = await storage.read_bytes_bounded(storage_key, min(TOOL_MATERIALIZE_MAX_FILE_BYTES, remaining))
        except StorageReadLimitExceededError:
            if remaining >= TOOL_MATERIALIZE_MAX_FILE_BYTES:
                raise
            return
        target = _safe_local_target(local_root, rel_path)
        if target is None:
            return
        base_hash = await anyio.to_thread.run_sync(_write_local_file, target, data)
        normalized_rel = normalize_workspace_path(rel_path)
        manifest[normalized_rel] = TempWorkspaceManifestEntry(
            rel_path=normalized_rel,
            storage_key=storage_key,
            base_version_token=version.token,
            base_hash=base_hash,
            size=len(data),
        )
        budget["total"] += len(data)
        return
    if await storage.is_dir(storage_key):
        target = _safe_local_target(local_root, rel_path)
        if target is None:
            return
        target.mkdir(parents=True, exist_ok=True)
        for entry in await storage.list_dir(storage_key):
            child_rel = f"{rel_path.rstrip('/')}/{entry.name}" if rel_path else entry.name
            await _materialize_storage_path_with_budget(storage, entry.key, child_rel, local_root, budget, manifest)


async def flush_temp_workspace(
    temp_workspace: TempWorkspace,
    conflict_mode: str = "fail",
) -> FlushTempWorkspaceResult:
    """Flush local changes back to storage using manifest-based conflict checks."""

    storage = get_storage_backend()
    selected_paths = [normalize_workspace_path(path) for path in temp_workspace.selected_paths]
    manifest = temp_workspace.manifest
    local_files = _collect_temp_workspace_files(temp_workspace.root, selected_paths)

    updated: list[str] = []
    conflicted: list[str] = []
    deleted: list[str] = []
    skipped: list[str] = []

    try:
        async with workspace_locks(temp_workspace.agent_id, selected_paths):
            for rel_path, local_path in local_files.items():
                if local_path.name.startswith("_exec_tmp") or "__pycache__" in local_path.parts:
                    continue
                entry = manifest.get(rel_path)
                digest = await anyio.to_thread.run_sync(_digest_local_file, local_path)
                if digest is not None and entry and entry.base_hash == digest:
                    skipped.append(rel_path)
                    continue
                data = None if digest is None else await anyio.to_thread.run_sync(_read_local_file, local_path)
                if data is None:
                    logger.warning(f"[temp-workspace] {rel_path} exceeds {TOOL_MATERIALIZE_MAX_FILE_BYTES} bytes")
                    conflicted.append(rel_path)
                    if conflict_mode == "fail":
                        return {"updated": updated, "deleted": deleted, "conflicted": conflicted, "skipped": skipped}
                    continue
                condition = (
                    WriteCondition(version_token=entry.base_version_token)
                    if entry
                    else WriteCondition(require_absent=True)
                )
                storage_key = (
                    entry.storage_key if entry else normalize_storage_key(f"{temp_workspace.agent_id}/{rel_path}")
                )
                result = await storage.write_bytes_if_match(
                    storage_key,
                    data,
                    condition=condition,
                )
                if not result.ok:
                    conflicted.append(rel_path)
                    if conflict_mode == "fail":
                        return {"updated": updated, "deleted": deleted, "conflicted": conflicted, "skipped": skipped}
                    continue
                updated.append(rel_path)

            for rel_path, entry in manifest.items():
                if rel_path in local_files:
                    continue
                if _safe_local_target(temp_workspace.root, rel_path) is None:
                    skipped.append(rel_path)
                    continue
                result = await storage.delete_if_match(
                    entry.storage_key,
                    condition=WriteCondition(version_token=entry.base_version_token),
                )
                if not result.ok:
                    conflicted.append(rel_path)
                    if conflict_mode == "fail":
                        return {"updated": updated, "deleted": deleted, "conflicted": conflicted, "skipped": skipped}
                    continue
                deleted.append(rel_path)
    finally:
        changed = [*updated, *deleted]
        if changed:
            from app.services.agent_context_cache import invalidate_for_workspace_paths

            await invalidate_for_workspace_paths(temp_workspace.agent_id, *changed)

    return {"updated": updated, "deleted": deleted, "conflicted": conflicted, "skipped": skipped}


def _digest_local_file(path: Path) -> str | None:
    """Hash a file in chunks, or ``None`` once it holds more than the per-file cap; never reads an oversize file."""
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        if os.fstat(stream.fileno()).st_size > TOOL_MATERIALIZE_MAX_FILE_BYTES:
            return None
        while chunk := stream.read(_FLUSH_CHUNK_BYTES):
            total += len(chunk)
            if total > TOOL_MATERIALIZE_MAX_FILE_BYTES:
                return None
            digest.update(chunk)
    return digest.hexdigest()


def _read_local_file(path: Path) -> bytes | None:
    """The file's bytes, or ``None`` when it holds more than the per-file cap."""
    with path.open("rb") as stream:
        try:
            return read_bounded(stream, TOOL_MATERIALIZE_MAX_FILE_BYTES)
        except StorageReadLimitExceededError:
            return None


def _collect_temp_workspace_files(root: Path, selected_paths: list[str]) -> dict[str, Path]:
    files: dict[str, Path] = {}
    root_resolved = root.resolve()
    for selected in selected_paths:
        if not selected:
            continue
        target = (root_resolved / selected).resolve()
        if not target.is_relative_to(root_resolved):
            continue
        if target.is_file():
            files[normalize_workspace_path(selected)] = target
            continue
        if not target.exists() or not target.is_dir():
            continue
        for path in target.rglob("*"):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root_resolved):
                continue
            rel = resolved.relative_to(root_resolved).as_posix()
            files[normalize_workspace_path(rel)] = path
    return files


def _is_enterprise_info_path(path: str | None) -> bool:
    normalized = str(path or "").replace("\\", "/").strip().strip("/")
    return normalized == "enterprise_info" or normalized.startswith("enterprise_info/")
