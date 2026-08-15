"""Agent-scoped tokens for the Linkup proxy (not real Linkup secrets)."""

from __future__ import annotations

import hmac
from hashlib import sha256
from uuid import UUID

from app.config import get_settings


def make_proxy_token(agent_id: UUID, *, secret: str | None = None) -> str:
    key = (secret if secret is not None else get_settings().SECRET_KEY).encode("utf-8")
    digest = hmac.new(key, f"linkup-proxy:{agent_id}".encode("utf-8"), sha256).hexdigest()
    return f"{agent_id}.{digest}"


def parse_proxy_token(token: str, *, secret: str | None = None) -> UUID | None:
    cleaned = token.strip()
    if "." not in cleaned:
        return None
    raw_id, digest = cleaned.split(".", 1)
    try:
        agent_id = UUID(raw_id)
    except ValueError:
        return None
    expected = make_proxy_token(agent_id, secret=secret)
    if not hmac.compare_digest(expected, f"{agent_id}.{digest}"):
        return None
    return agent_id
