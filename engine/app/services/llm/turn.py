"""Preloaded rows for one chat/IM/worker LLM turn."""

from __future__ import annotations

from dataclasses import dataclass

from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
from app.records.user import UserRecord


@dataclass(slots=True)
class TurnContext:
    """Agent/models already loaded by the caller (WS handshake, invoker, …)."""

    agent: AgentRecord | None = None
    primary_model: LLMModelRecord | None = None
    fallback_model: LLMModelRecord | None = None
    user: UserRecord | None = None
    user_name: str | None = None
