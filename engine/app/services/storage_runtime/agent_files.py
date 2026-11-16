"""Agent-scoped storage helpers.

This module centralizes how agent and tenant workspace keys are built so
channel handlers and background services do not manually assemble
`workspace/uploads/...` paths all over the codebase.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, override
from unicodedata import category

from app.services.storage_runtime.facade import get_storage_backend, guess_content_type
from app.services.storage_runtime.utils import normalize_storage_key


@dataclass(frozen=True, slots=True)
class UploadStorageError(RuntimeError):
    code: Literal["create_not_confirmed", "filename_collision"]

    @override
    def __str__(self) -> str:
        return self.code


def sanitize_filename(filename: str, fallback: str = "file.bin") -> str:
    name = "".join("_" if char in "/\\" or category(char) == "Cc" else char for char in filename).strip()
    if not name or name in (".", ".."):
        return fallback
    suffix = Path(name.lstrip(".")).suffix
    if len(suffix.encode("utf-8")) > 32:
        suffix = ""
    stem = name.removesuffix(suffix)
    return stem.encode("utf-8")[: 240 - len(suffix.encode("utf-8"))].decode("utf-8", errors="ignore") + suffix


def agent_storage_key(agent_id: uuid.UUID | str, rel_path: str = "") -> str:
    prefix = str(agent_id)
    rel = normalize_storage_key(rel_path)
    return f"{prefix}/{rel}" if rel else prefix


def agent_workspace_key(agent_id: uuid.UUID | str, rel_path: str = "") -> str:
    rel = normalize_storage_key(rel_path)
    workspace_rel = f"workspace/{rel}" if rel else "workspace"
    return agent_storage_key(agent_id, workspace_rel)


def agent_upload_key(agent_id: uuid.UUID | str, filename: str) -> str:
    safe_name = sanitize_filename(filename)
    return agent_workspace_key(agent_id, f"uploads/{safe_name}")


def tenant_storage_key(tenant_id: uuid.UUID | str, rel_path: str = "") -> str:
    prefix = normalize_storage_key(f"enterprise_info_{tenant_id}")
    rel = normalize_storage_key(rel_path)
    return f"{prefix}/{rel}" if rel else prefix


async def store_agent_bytes(
    agent_id: uuid.UUID | str,
    rel_path: str,
    data: bytes,
    *,
    content_type: str | None = None,
) -> str:
    key = agent_storage_key(agent_id, rel_path)
    storage = get_storage_backend()
    await storage.write_bytes(
        key,
        data,
        content_type=content_type or guess_content_type(Path(rel_path).name),
    )
    return key


async def store_agent_upload(
    agent_id: uuid.UUID | str,
    filename: str,
    data: bytes,
    *,
    content_type: str | None = None,
) -> tuple[str, str, None]:
    """Store an upload and return ``(key, workspace_path, None)``.

    No caller reads a local path, so none is materialized; the third slot only keeps existing unpacking intact.
    """
    storage = get_storage_backend()
    safe_name = sanitize_filename(filename)
    suffix = Path(safe_name.lstrip(".")).suffix
    stem = safe_name.removesuffix(suffix)
    for counter in range(100):
        candidate = safe_name if counter == 0 else f"{stem}_{counter}{suffix}"
        key = agent_workspace_key(agent_id, f"uploads/{candidate}")
        result = await storage.create_bytes_if_absent(key, data, content_type or guess_content_type(candidate))
        if result.ok:
            return key, f"workspace/uploads/{candidate}", None
        if not result.conflict:
            raise UploadStorageError("create_not_confirmed")
    raise UploadStorageError("filename_collision")
