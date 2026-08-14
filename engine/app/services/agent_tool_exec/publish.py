"""Public HTML page publishing tool behavior."""

import os
import re
import secrets
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath

from app.dao.agent_dao import agent_dao
from app.dao.published_page_dao import published_page_dao
from app.services.agent_tool_exec.registry import ToolArguments
from app.services.storage_runtime import get_storage_backend, normalize_storage_key


def _published_page_storage_key(agent_id: uuid.UUID, path: str) -> str | None:
    """Return a normalized storage key only when it remains under the agent root."""
    posix_path = PurePosixPath(path.replace("\\", "/"))
    windows_path = PureWindowsPath(path)
    raw_parts = path.replace("\\", "/").split("/")
    if posix_path.is_absolute() or windows_path.is_absolute() or any(part in {".", ".."} for part in raw_parts):
        return None

    agent_prefix = normalize_storage_key(str(agent_id))
    storage_key = normalize_storage_key(f"{agent_prefix}/{path}")
    agent_parts = PurePosixPath(agent_prefix).parts
    storage_parts = PurePosixPath(storage_key).parts
    if storage_parts[: len(agent_parts)] != agent_parts or len(storage_parts) == len(agent_parts):
        return None
    return storage_key


async def _publish_page(agent_id: uuid.UUID, user_id: uuid.UUID, ws: Path, arguments: ToolArguments) -> str:
    """Publish an HTML file as a public page."""
    _ = ws
    path_value = arguments.get("path", "")
    path = path_value if isinstance(path_value, str) else ""
    if not path:
        return "Missing required argument 'path'"

    if not path.lower().endswith((".html", ".htm")):
        return "Only .html and .htm files can be published"

    storage_key = _published_page_storage_key(agent_id, path)
    if storage_key is None:
        return f"Invalid path: {path}"

    storage = get_storage_backend()
    if not await storage.exists(storage_key) or not await storage.is_file(storage_key):
        return f"File not found: {path}"

    try:
        content = await storage.read_text(storage_key, encoding="utf-8", errors="replace")
        title_match = re.search(r"<title[^>]*>(.*?)</title>", content, re.IGNORECASE | re.DOTALL)
        title = title_match.group(1).strip()[:200] if title_match else Path(path).stem
    except Exception:
        title = Path(path).stem

    short_id = secrets.token_urlsafe(6)[:8]
    tenant_id = None
    try:
        agent = await agent_dao.get(agent_id)
        tenant_id = agent.tenant_id if agent else None
    except Exception:
        tenant_id = None

    try:
        _ = await published_page_dao.create(
            obj_in={
                "short_id": short_id,
                "agent_id": agent_id,
                "user_id": user_id,
                "tenant_id": tenant_id,
                "source_path": path,
                "title": title,
                "view_count": 0,
            }
        )
    except Exception as e:
        return f"Failed to publish: {e}"

    try:
        from app.config import get_settings as _get_publish_settings

        public_base = (_get_publish_settings().PUBLIC_BASE_URL or os.environ.get("PUBLIC_BASE_URL", "")).rstrip("/")
    except Exception:
        public_base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not public_base:
        url = f"/p/{short_id}"
        url_note = (
            "\n\n> Note: PUBLIC_BASE_URL is not configured on this server. "
            + "The link above is a relative path - prepend your server's domain "
            + "to get the full URL. Set PUBLIC_BASE_URL in your .env to have "
            + "the agent generate complete links automatically."
        )
    else:
        url = f"{public_base}/p/{short_id}"
        url_note = ""

    return (
        "Published successfully!\n\n"
        + f"Public URL: {url}\n"
        + f"Title: {title}\n\n"
        + f"Anyone can access this page without logging in.{url_note}"
    )


async def _list_published_pages(agent_id: uuid.UUID) -> str:
    """List all published pages for this agent."""
    try:
        from app.config import get_settings as _get_publish_settings

        public_base = (_get_publish_settings().PUBLIC_BASE_URL or os.environ.get("PUBLIC_BASE_URL", "")).rstrip("/")
    except Exception:
        public_base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

    try:
        pages = await published_page_dao.list_for_agent(agent_id)

        if not pages:
            return "No published pages yet."

        lines = [f"Published pages ({len(pages)} total):\n"]
        for page in pages:
            url = f"{public_base}/p/{page.short_id}" if public_base else f"/p/{page.short_id}"
            lines.append(f"- {page.title or 'Untitled'}")
            lines.append(f"  URL: {url}")
            lines.append(f"  Source: {page.source_path}")
            lines.append(f"  Views: {page.view_count}")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"Failed to list pages: {e}"
