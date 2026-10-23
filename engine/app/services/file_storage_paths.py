"""Canonical logical storage keys shared by file downloads and previews."""

import uuid

from fastapi import HTTPException

from app.services.file_download_tickets import normalize_download_path
from app.services.storage_runtime.utils import normalize_storage_key


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


def visible_storage_key(agent_id: uuid.UUID, rel_path: str, tenant_id: uuid.UUID | None) -> tuple[str, bool]:
    normalized = normalize_download_path(rel_path)
    if is_enterprise_visible_path(normalized):
        if not tenant_id:
            raise HTTPException(status_code=400, detail="No tenant associated")
        sub_path = normalized[len("enterprise_info") :].lstrip("/")
        return enterprise_storage_key(str(tenant_id), sub_path), True
    return agent_storage_key(agent_id, normalized), False
