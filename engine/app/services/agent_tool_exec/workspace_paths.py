from __future__ import annotations

import re
import unicodedata
import uuid
from pathlib import Path


async def _get_agent_tenant_id(
    agent_id: uuid.UUID,
    *,
    async_session_factory=None,
    select_fn=None,
    agent_model=None,
) -> str | None:
    """Resolve agent tenant id via DAO. Legacy kwargs accepted and ignored."""
    del async_session_factory, select_fn, agent_model
    try:
        from app.dao.agent_dao import agent_dao

        agent = await agent_dao.get(agent_id)
        if agent and agent.tenant_id:
            return str(agent.tenant_id)
    except Exception:
        return None
    return None


def _agent_workspace_root(agent_id: uuid.UUID, *, workspace_root: Path) -> Path:
    return workspace_root / str(agent_id)


def _normalize_tool_rel_path(rel_path: str) -> str:
    normalized = unicodedata.normalize("NFC", (rel_path or "").strip()).replace("\\", "/")
    return re.sub(r"/+", "/", normalized).lstrip("./")


def _collapse_filename_for_match(name: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", name or "")).casefold()


def _allowed_root_for_tool_path(
    ws: Path,
    rel_path: str,
    tenant_id: str | None = None,
    *,
    workspace_root: Path,
    normalize_tool_rel_path,
) -> tuple[Path, str]:
    normalized = normalize_tool_rel_path(rel_path)
    if normalized.startswith("enterprise_info"):
        enterprise_root = (
            (workspace_root / f"enterprise_info_{tenant_id}").resolve()
            if tenant_id
            else (workspace_root / "enterprise_info").resolve()
        )
        sub = normalized[len("enterprise_info") :].lstrip("/")
        return enterprise_root, sub
    return ws.resolve(), normalized


def _resolve_tool_source_path(
    ws: Path,
    rel_path: str,
    tenant_id: str | None = None,
    *,
    allowed_root_for_tool_path,
    collapse_filename_for_match,
) -> Path:
    root, normalized = allowed_root_for_tool_path(ws, rel_path, tenant_id=tenant_id)
    candidate = (root / normalized).resolve() if normalized else root
    if not candidate.is_relative_to(root):
        raise ValueError("Access denied for this path")
    if candidate.exists():
        return candidate

    parent = candidate.parent
    if parent.exists():
        wanted = collapse_filename_for_match(candidate.name)
        for sibling in parent.iterdir():
            if collapse_filename_for_match(sibling.name) == wanted:
                return sibling
    return candidate


def _resolve_tool_target_path(
    ws: Path,
    rel_path: str,
    tenant_id: str | None = None,
    *,
    allowed_root_for_tool_path,
) -> Path:
    root, normalized = allowed_root_for_tool_path(ws, rel_path, tenant_id=tenant_id)
    candidate = (root / normalized).resolve() if normalized else root
    if not candidate.is_relative_to(root):
        raise ValueError("❌ Access denied.")
    return candidate


def _tool_storage_key(
    agent_id: uuid.UUID,
    rel_path: str,
    tenant_id: str | None = None,
    *,
    normalize_workspace_path_fn,
    normalize_tool_rel_path,
    is_enterprise_info_path,
    normalize_storage_key_fn,
) -> tuple[str, str, bool]:
    normalized = normalize_workspace_path_fn(normalize_tool_rel_path(rel_path))
    if is_enterprise_info_path(normalized):
        if not tenant_id:
            key = "enterprise_info/" + normalized.removeprefix("enterprise_info").lstrip("/")
            return normalize_storage_key_fn(key), normalized, True
        sub = normalized[len("enterprise_info") :].lstrip("/")
        key = f"enterprise_info_{tenant_id}/{sub}" if sub else f"enterprise_info_{tenant_id}"
        return normalize_storage_key_fn(key), normalized, True
    key = f"{agent_id}/{normalized}" if normalized else str(agent_id)
    return normalize_storage_key_fn(key), normalized, False
