from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.agentbay_client import AgentBayResponseValue


def _agentbay_response_text(value: AgentBayResponseValue, fallback: str) -> str:
    return value if isinstance(value, str) else fallback


def _agentbay_response_list(value: AgentBayResponseValue) -> list[Any]:
    return list(value) if isinstance(value, list) else []
