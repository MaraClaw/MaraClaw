"""Complexity routing for Docker-managed OpenClaw guests.

The guest LLM is whatever ``openclaw.json`` currently names. Classify in the
engine (same ``select_turn_model`` as native), persist the choice on the
gateway message, then rewrite the guest primary to the fail-closed applied
model so the next heartbeat runs on that model. Fallback stays failover-only.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from app.core.logging import logger
from app.dao.gateway_message_dao import gateway_message_dao
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.llm import LLMModelRecord
from app.services.agent_manager import agent_manager, guest_model_ref
from app.services.llm.router import load_agent_model_bundle, select_turn_model
from app.services.llm.turn import ModelBundle, ModelSlot
from app.services.llm.types import OpenAIMessage


def requires_primary(messages: Sequence[GatewayMessageRecord]) -> bool:
    """True when any queued row is missing a slot or is classified primary."""
    return any((message.selected_slot or "primary") == "primary" for message in messages)


def poll_model_hint(messages: Sequence[GatewayMessageRecord]) -> tuple[str | None, str | None]:
    """Fail-closed model hint for one poll batch.

    Mixed primary+secondary batches advertise primary so the guest does not
    downgrade while a complex turn is still waiting.
    """
    if not messages:
        return None, None
    if requires_primary(messages):
        ref = next(
            (
                message.guest_model_ref
                for message in messages
                if (message.selected_slot or "primary") == "primary" and message.guest_model_ref
            ),
            None,
        )
        return ref, "primary"
    ref = next((message.guest_model_ref for message in messages if message.guest_model_ref), None)
    return ref, "secondary"


def applied_guest_model(
    choice_slot: ModelSlot,
    choice_model: LLMModelRecord | None,
    bundle: ModelBundle,
    pending: Sequence[GatewayMessageRecord],
) -> LLMModelRecord | None:
    """Model to write into ``openclaw.json`` for this enqueue."""
    if choice_slot == "secondary" and requires_primary(pending):
        return bundle.primary or choice_model
    return choice_model


async def enqueue_openclaw_message(
    *,
    agent: AgentRecord,
    content: str,
    sender_user_id: UUID | None = None,
    sender_agent_id: UUID | None = None,
    conversation_id: str | None = None,
    history: list[OpenAIMessage] | None = None,
) -> GatewayMessageRecord:
    """Classify ``content``, rewrite guest config, and queue the gateway row."""
    bundle = await load_agent_model_bundle(agent)
    choice = await select_turn_model(
        bundle,
        user_text=content,
        history=history,
        agent_id=agent.id,
    )
    pending = await gateway_message_dao.list_pending(agent.id)
    apply_model = applied_guest_model(choice.slot, choice.model, bundle, pending)
    try:
        _ = agent_manager.write_guest_config(
            agent,
            primary=bundle.primary,
            secondary=bundle.secondary,
            fallback=bundle.fallback,
            selected=apply_model,
        )
    except Exception as exc:
        logger.warning("[OpenClaw] guest config write failed for {}: {}", agent.id, exc)

    created = await gateway_message_dao.create(
        obj_in={
            "agent_id": agent.id,
            "sender_user_id": sender_user_id,
            "sender_agent_id": sender_agent_id,
            "conversation_id": conversation_id,
            "content": content,
            "status": "pending",
            "selected_slot": choice.slot,
            "guest_model_ref": guest_model_ref(choice.model),
            "complexity": choice.complexity,
            "routing_reason": choice.reason,
        }
    )
    logger.info(
        "[OpenClaw] queued agent={} slot={} reason={} guest_model={}",
        agent.id,
        choice.slot,
        choice.reason,
        guest_model_ref(choice.model),
    )
    return created
