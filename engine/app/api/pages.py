"""Public pages API - serves published HTML without authentication."""

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.dao.published_page_dao import published_page_dao
from app.records.published_page import PublishedPageRecord
from app.records.user import UserRecord
from app.services.storage import get_storage_backend, normalize_storage_key

# Public router - no /api prefix, no auth
public_router = APIRouter(tags=["pages"])

# Authenticated router - under /api prefix
router = APIRouter(prefix="/pages", tags=["pages"])

# ── Public render (NO auth) ────────────────────────────


@public_router.get("/p/{short_id}")
async def render_page(short_id: str):
    """Serve a published HTML page. No authentication required."""
    page = await published_page_dao.get_by_short_id(short_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    storage = get_storage_backend()
    storage_key = normalize_storage_key(f"{page.agent_id}/{page.source_path}")
    if not await storage.exists(storage_key) or not await storage.is_file(storage_key):
        raise HTTPException(status_code=404, detail="Source file no longer exists")

    html_content = await storage.read_text(storage_key, encoding="utf-8", errors="replace")

    await published_page_dao.increment_view_count(page.id)

    return HTMLResponse(
        content=html_content,
        headers={
            # CSP sandbox: isolates origin, prevents access to parent localStorage/cookies
            "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups allow-modals",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ── Authenticated endpoints ────────────────────────────


class PagePublish(BaseModel):
    agent_id: uuid.UUID
    path: str = Field(..., min_length=1, max_length=500)


def _page_payload(page: PublishedPageRecord) -> dict[str, Any]:
    return {
        "id": str(page.id),
        "short_id": page.short_id,
        "source_path": page.source_path,
        "title": page.title,
        "view_count": page.view_count,
        "created_at": page.created_at.isoformat() if page.created_at else None,
        "url": f"/p/{page.short_id}",
    }


def _require_manage(current_user: UserRecord, access_level: str) -> None:
    if access_level == "manage" or current_user.role in ("platform_admin", "org_admin"):
        return
    raise HTTPException(status_code=403, detail="Manage access required")


@router.get("/list")
async def list_pages(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)) -> list[dict[str, Any]]:
    """List published pages for an agent."""
    _ = await check_agent_access(current_user, agent_id)

    pages = await published_page_dao.list_for_agent(agent_id)
    return [_page_payload(p) for p in pages]


@router.post("/")
async def publish_page(body: PagePublish, current_user: UserRecord = Depends(get_current_user)) -> dict[str, Any]:
    """Publish an HTML workspace file as a public /p/{short_id} page."""
    from app.services.agent_tool_exec.publish import _publish_page

    _agent, access_level = await check_agent_access(current_user, body.agent_id)
    _require_manage(current_user, access_level)

    result = await _publish_page(body.agent_id, current_user.id, Path("."), {"path": body.path})
    if not result.startswith("Published successfully"):
        raise HTTPException(status_code=400, detail=result)

    pages = await published_page_dao.list_for_agent(body.agent_id)
    published = next((p for p in pages if p.source_path == body.path), pages[0] if pages else None)
    if published is None:
        raise HTTPException(status_code=500, detail="Page published but could not be loaded")
    return _page_payload(published)


@router.delete("/{page_id}")
async def unpublish_page(page_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)) -> dict[str, bool]:
    """Remove a published page. The workspace HTML file is left in place."""
    page = await published_page_dao.get(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    _agent, access_level = await check_agent_access(current_user, page.agent_id)
    _require_manage(current_user, access_level)
    _ = await published_page_dao.delete(id=page_id)
    return {"deleted": True}
