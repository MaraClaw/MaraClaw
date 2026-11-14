"""Preloaded rows for one chat/IM/worker LLM turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
from app.records.user import UserRecord

ModelSlot = Literal["primary", "fallback"]


@dataclass(slots=True)
class ModelBundle:
    """The two assigned models for one agent, before routing."""

    primary: LLMModelRecord | None = None
    fallback: LLMModelRecord | None = None


@dataclass(slots=True)
class TurnContext:
    """Agent/models already loaded by the caller (WS handshake, invoker, …)."""

    agent: AgentRecord | None = None
    primary_model: LLMModelRecord | None = None
    fallback_model: LLMModelRecord | None = None
    selected_model: LLMModelRecord | None = None
    selected_slot: ModelSlot | None = None
    routing_reason: str | None = None
    user: UserRecord | None = None
    user_name: str | None = None
