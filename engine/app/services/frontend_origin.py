"""Resolve the member/admin frontend origin for email links."""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import Request

from app.config import get_settings


def origin_from_request(request: Request | None) -> str | None:
    """Return an allowlisted frontend Origin, or None if it is missing/unknown.

    Uses the Origin header, then the Referer origin. Unknown values are ignored
    so reset/verify emails cannot be pointed at an attacker host.
    """
    if request is None:
        return None

    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        referer = (request.headers.get("referer") or "").strip()
        if referer:
            parsed = urlparse(referer)
            if parsed.scheme and parsed.netloc:
                origin = f"{parsed.scheme}://{parsed.netloc}"
    if not origin:
        return None

    normalized = origin.rstrip("/")
    allowed = {item.rstrip("/") for item in get_settings().CORS_ORIGINS}
    if normalized in allowed:
        return normalized
    return None


async def resolve_frontend_base_url(request: Request | None = None) -> str:
    """Prefer the allowlisted request Origin; otherwise PUBLIC_BASE_URL."""
    origin = origin_from_request(request)
    if origin:
        return origin
    from app.services.platform_service import platform_service

    return await platform_service.get_public_base_url()
