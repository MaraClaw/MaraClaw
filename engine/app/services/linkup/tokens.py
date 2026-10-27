"""Agent-scoped tokens for the Linkup proxy (not real Linkup secrets)."""

from __future__ import annotations

import hmac
from hashlib import sha256
from uuid import UUID

from app.config import get_settings
from app.dao.linkup_proxy_dao import save_proxy_token


def _proxy_token(agent_id: UUID, *, secret: str | None = None) -> str:
    key = (secret if secret is not None else get_settings().SECRET_KEY).encode("utf-8")
    digest = hmac.new(key, f"linkup-proxy:{agent_id}".encode(), sha256).hexdigest()
    return f"{agent_id}.{digest}"


async def make_proxy_token(agent_id: UUID, *, secret: str | None = None) -> str:
    token = _proxy_token(agent_id, secret=secret)
    await save_proxy_token(agent_id, sha256(token.encode()).digest())
    return token


def parse_proxy_token(token: str, *, secret: str | None = None) -> UUID | None:
    cleaned = token.strip()
    if "." not in cleaned:
        return None
    raw_id, digest = cleaned.split(".", 1)
    try:
        agent_id = UUID(raw_id)
    except ValueError:
        return None
    expected = _proxy_token(agent_id, secret=secret)
    presented = f"{agent_id}.{digest}"
    try:
        if not hmac.compare_digest(expected, presented):
            return None
    except TypeError, ValueError:
        return None
    return agent_id
