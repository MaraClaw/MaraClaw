"""Canonical logical storage keys shared by file downloads and previews."""

import uuid
from pathlib import Path

from fastapi import HTTPException

from app.config import get_settings
from app.services.file_download_tickets import normalize_download_path
from app.services.storage_runtime.utils import normalize_storage_key

# Root entries members may reach; everything else at the agent root is guest runtime state (keys, tokens, gateway config).
MEMBER_VISIBLE_ROOT_ENTRIES = frozenset(
    {
        "AGENTS.md",
        "HEARTBEAT.md",
        "soul.md",
        "state.json",
        "focus.md",
        "agenda.md",
        "tasks.json",
        "todo.json",
        "daily_reports",
        "memory",
        "skills",
        "workspace",
        "media",
        "enterprise_info",
    }
)


def agent_storage_key(agent_id: uuid.UUID, rel_path: str = "") -> str:
    prefix = str(agent_id)
    rel = normalize_storage_key(rel_path)
    return f"{prefix}/{rel}" if rel else prefix


def enterprise_storage_key(tenant_id: str, rel_path: str = "") -> str:
    prefix = f"enterprise_info_{tenant_id}"
    rel = normalize_storage_key(rel_path)
    return f"{prefix}/{rel}" if rel else prefix


def is_enterprise_visible_path(rel_path: str) -> bool:
    normalized = rel_path.strip().strip("/")
    return normalized == "enterprise_info" or normalized.startswith("enterprise_info/")


def member_path(rel_path: str) -> str:
    """The agent-relative path a member route acts on: stripped, with `.` and `..` collapsed like storage keys."""
    return normalize_storage_key(normalize_download_path(rel_path))


def is_member_visible_path(rel_path: str) -> bool:
    """Whether a member may reach this agent-relative path; the first segment is checked after normalizing."""
    first_segment = member_path(rel_path).partition("/")[0]
    return not first_segment or first_segment in MEMBER_VISIBLE_ROOT_ENTRIES


def _crosses_symlink(agent_id: uuid.UUID, normalized: str) -> bool:
    settings = get_settings()
    requested = Path(settings.STORAGE_LOCAL_ROOT or settings.AGENT_DATA_DIR).resolve() / str(agent_id) / normalized
    try:
        return requested.resolve() != requested
    except OSError, RuntimeError:
        return True


def is_member_reachable(agent_id: uuid.UUID, rel_path: str) -> bool:
    """Whether the path is under an allowlisted root entry and reaches it without crossing a symlink.

    The guest writes its own directory, and local storage follows links that stay inside the storage root, so a link
    the guest plants (``workspace/up -> ..``) would otherwise lead a member route to runtime files or another agent.
    """
    normalized = member_path(rel_path)
    return is_member_visible_path(normalized) and (
        is_enterprise_visible_path(normalized) or not _crosses_symlink(agent_id, normalized)
    )


def require_member_visible_path(agent_id: uuid.UUID, rel_path: str) -> None:
    if not is_member_reachable(agent_id, rel_path):
        raise HTTPException(status_code=404, detail="Path not found")


def visible_storage_key(agent_id: uuid.UUID, rel_path: str, tenant_id: uuid.UUID | None) -> tuple[str, bool]:
    normalized = member_path(rel_path)
    require_member_visible_path(agent_id, normalized)
    if is_enterprise_visible_path(normalized):
        if not tenant_id:
            raise HTTPException(status_code=400, detail="No tenant associated")
        sub_path = normalized[len("enterprise_info") :].lstrip("/")
        return enterprise_storage_key(str(tenant_id), sub_path), True
    return agent_storage_key(agent_id, normalized), False
