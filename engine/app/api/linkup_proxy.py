"""Guest-facing Linkup proxy: rotate stored keys on quota errors."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response

from app.services.linkup.client import LinkupProxyError, allowed_upstream_path, proxy_linkup
from app.services.linkup.tokens import parse_proxy_token

router = APIRouter(prefix="/api/linkup", tags=["linkup"])


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return authorization.strip()
    return token.strip()


@router.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_v1(
    path: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    if not allowed_upstream_path(path):
        raise HTTPException(status_code=404, detail="Unknown Linkup path")
    token = _bearer_token(authorization)
    if parse_proxy_token(token) is None:
        raise HTTPException(status_code=401, detail="Invalid Linkup proxy token")

    body = await request.body()
    headers = {key: value for key, value in request.headers.items() if key.lower() != "authorization"}
    try:
        status, text, out_headers = await proxy_linkup(
            method=request.method,
            path=path,
            headers=headers,
            content=body or None,
        )
    except LinkupProxyError as exc:
        return Response(content=exc.body, status_code=exc.status_code, media_type="application/json")
    media = out_headers.get("content-type") or "application/json"
    return Response(content=text, status_code=status, media_type=media)
