"""Public pages API — serves published HTML without authentication."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from app.core.security import get_current_user
from app.dao.published_page_dao import published_page_dao
from app.records.user import UserRecord
from app.services.storage import get_storage_backend, normalize_storage_key

# Public router — no /api prefix, no auth
public_router = APIRouter(tags=["pages"])

# Authenticated router — under /api prefix
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


@router.get("/list")
async def list_pages(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """List published pages for an agent."""
    from app.core.permissions import check_agent_access

    await check_agent_access(current_user, agent_id)

    pages = await published_page_dao.list_for_agent(agent_id)
    return [
        {
            "id": str(p.id),
            "short_id": p.short_id,
            "source_path": p.source_path,
            "title": p.title,
            "view_count": p.view_count,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "url": f"/p/{p.short_id}",
        }
        for p in pages
    ]
