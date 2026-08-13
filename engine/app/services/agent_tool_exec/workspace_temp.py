from __future__ import annotations

import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.services.storage import get_storage_backend, normalize_storage_key
from app.services.storage_runtime.base import StorageBackend, WriteCondition, content_hash_bytes
from app.services.workspace_collaboration import normalize_workspace_path
from app.services.workspace_locking import workspace_locks

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
    if not str(target).startswith(str(root_resolved)):
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
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(await storage.read_bytes(entry_key))


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
    for rel_path in selected:
        storage_key, normalized, is_enterprise = agent_tools._tool_storage_key(agent_id, rel_path, tenant_id)
        if is_enterprise:
            continue
        await _materialize_storage_path_with_budget(storage, storage_key, normalized, temp_ws, budget, manifest)
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
        version = await storage.get_version(storage_key)
        if version.size > TOOL_MATERIALIZE_MAX_FILE_BYTES:
            return
        if budget["total"] + version.size > TOOL_MATERIALIZE_MAX_TOTAL_BYTES:
            return
        target = _safe_local_target(local_root, rel_path)
        if target is None:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        data = await storage.read_bytes(storage_key)
        target.write_bytes(data)
        normalized_rel = normalize_workspace_path(rel_path)
        manifest[normalized_rel] = TempWorkspaceManifestEntry(
            rel_path=normalized_rel,
            storage_key=storage_key,
            base_version_token=version.token,
            base_hash=content_hash_bytes(data),
            size=version.size,
        )
        budget["total"] += version.size
        return
    if await storage.is_dir(storage_key):
        (local_root / rel_path).mkdir(parents=True, exist_ok=True)
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

    async with workspace_locks(temp_workspace.agent_id, selected_paths):
        for rel_path, local_path in local_files.items():
            if local_path.name.startswith("_exec_tmp") or "__pycache__" in local_path.parts:
                continue
            data = local_path.read_bytes()
            current_hash = content_hash_bytes(data)
            entry = manifest.get(rel_path)
            if entry and entry.base_hash == current_hash:
                skipped.append(rel_path)
                continue
            condition = (
                WriteCondition(version_token=entry.base_version_token) if entry else WriteCondition(require_absent=True)
            )
            storage_key = entry.storage_key if entry else normalize_storage_key(f"{temp_workspace.agent_id}/{rel_path}")
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

    return {"updated": updated, "deleted": deleted, "conflicted": conflicted, "skipped": skipped}


def _collect_temp_workspace_files(root: Path, selected_paths: list[str]) -> dict[str, Path]:
    files: dict[str, Path] = {}
    root_resolved = root.resolve()
    for selected in selected_paths:
        if not selected:
            continue
        target = (root_resolved / selected).resolve()
        if not str(target).startswith(str(root_resolved)):
            continue
        if target.is_file():
            files[normalize_workspace_path(selected)] = target
            continue
        if not target.exists() or not target.is_dir():
            continue
        for path in target.rglob("*"):
            if not path.is_file():
                continue
            rel = path.resolve().relative_to(root_resolved).as_posix()
            files[normalize_workspace_path(rel)] = path
    return files


def _is_enterprise_info_path(path: str | None) -> bool:
    normalized = str(path or "").replace("\\", "/").strip().strip("/")
    return normalized == "enterprise_info" or normalized.startswith("enterprise_info/")
