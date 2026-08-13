"""Preloaded rows for one chat/IM/worker LLM turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class TurnContext:
    """Agent/models already loaded by the caller (WS handshake, invoker, …)."""

    agent: Any | None = None
    primary_model: Any | None = None
    fallback_model: Any | None = None
    user: Any | None = None
    user_name: str | None = None
