"""Guest-facing Tavily proxy: retry upstream failures before key failover, within request, response and time limits."""

from __future__ import annotations

from hashlib import sha256

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response

from app.config import get_settings
from app.dao.tavily_proxy_dao import agent_available
from app.services.tavily.client import TavilyProxyError, allowed_upstream_path, proxy_tavily
from app.services.tavily.errors import TavilyLimitError
from app.services.tavily.tokens import parse_proxy_token

router = APIRouter(prefix="/api/tavily", tags=["tavily"])


def _bearer_token(authorization: str | None) -> str:
    if authorization in (None, ""):
        return ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return authorization.strip()
    return token.strip()


async def _read_bounded_body(request: Request, limit: int) -> bytes:
    """Buffer the request body, rejecting with 413 as soon as it passes ``limit`` bytes.

    Chunked uploads carry no ``Content-Length`` and a declared length can lie, so the cap is enforced while reading.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="Tavily request body too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail="Tavily request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


@router.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_v1(
    path: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    if not allowed_upstream_path(path):
        raise HTTPException(status_code=404, detail="Unknown Tavily path")
    token = _bearer_token(authorization)
    agent_id = parse_proxy_token(token)
    availability = (
        None
        if agent_id is None
        else await agent_available(agent_id, sha256(f"{agent_id}.{token.split('.', 1)[1]}".encode()).digest())
    )
    if agent_id is None or availability is None:
        raise HTTPException(status_code=401, detail="Invalid Tavily proxy token")

    body = await _read_bounded_body(request, get_settings().TAVILY_PROXY_MAX_REQUEST_BYTES)
    headers = {key: value for key, value in request.headers.items() if key.lower() != "authorization"}
    try:
        status, text, out_headers = await proxy_tavily(
            method=request.method,
            path=path,
            headers=headers,
            content=body or None,
            agent_id=agent_id,
            tenant_id=availability.tenant_id,
        )
    except (TavilyProxyError, TavilyLimitError) as exc:
        return Response(content=exc.body, status_code=exc.status_code, media_type="application/json")
    media = out_headers.get("content-type") or "application/json"
    return Response(content=text, status_code=status, media_type=media)
