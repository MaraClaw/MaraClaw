"""Mint and persist OpenClaw gateway API keys (oc-…)."""

from __future__ import annotations

import hashlib
import secrets

from app.records.agent import AgentRecord
from app.services.agent_manager import agent_manager

GATEWAY_KEY_FILENAME = ".maraclaw-gateway-key"


def mint_openclaw_gateway_key() -> tuple[str, str]:
    """Return ``(raw_key, sha256_hex)``. Store only the hash in the database."""
    raw_key = f"oc-{secrets.token_urlsafe(32)}"
    return raw_key, hashlib.sha256(raw_key.encode()).hexdigest()


def write_gateway_api_key(agent: AgentRecord, raw_key: str) -> None:
    """Write the one-time key into the bind-mounted agent dir for the guest."""
    agent_dir = agent_manager._agent_dir(agent.id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / GATEWAY_KEY_FILENAME
    path.write_text(raw_key.strip(), encoding="utf-8")
    path.chmod(0o600)


def read_gateway_api_key(agent: AgentRecord) -> str | None:
    path = agent_manager._agent_dir(agent.id) / GATEWAY_KEY_FILENAME
    if not path.is_file():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None
