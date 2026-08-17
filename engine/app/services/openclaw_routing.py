"""Complexity routing for Docker-managed OpenClaw guests.

The guest LLM is whatever ``openclaw.json`` currently names. Classify in the
engine (``select_turn_model``), persist the choice on the
gateway message, then rewrite the guest primary to the fail-closed applied
model so the next heartbeat runs on that model. Fallback stays failover-only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from uuid import UUID

from app.core.logging import logger
from app.dao.gateway_message_dao import gateway_message_dao
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.llm import LLMModelRecord
from app.services.agent_manager import agent_manager, guest_model_ref
from app.services.enterprise_llm import ensure_agent_company_models
from app.services.llm.router import load_agent_model_bundle, select_turn_model
from app.services.llm.turn import ModelBundle, ModelSlot
from app.services.llm.types import OpenAIMessage
from app.services.openclaw_hot_cache import (
    get_cached_bundle,
    mark_ensured,
    recently_ensured,
    set_cached_bundle,
)


class NoCompanyModelError(RuntimeError):
    """The tenant pool has no usable model for this OpenClaw guest."""


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
    await_wake: bool = True,
) -> GatewayMessageRecord:
    """Classify ``content``, rewrite guest config, and queue the gateway row.

    Chat WS passes ``await_wake=False`` so the socket can ack immediately.
    Background wake still runs the guest turn.
    """
    if not recently_ensured(agent):
        agent = await ensure_agent_company_models(agent)
        mark_ensured(agent)
    bundle = get_cached_bundle(agent)
    if bundle is None:
        bundle = await load_agent_model_bundle(agent)
        set_cached_bundle(agent, bundle)
    if bundle.primary is None and bundle.secondary is None and bundle.fallback is None:
        raise NoCompanyModelError(
            "This company has no model assigned. Connect a Grok subscription or add a model in Admin → Models, "
            + "then set it as primary."
        )
    choice = await select_turn_model(
        bundle,
        user_text=content,
        history=history,
        agent_id=agent.id,
        skip_classifier=True,
    )
    pending = await gateway_message_dao.list_pending(agent.id)
    duplicate = next(
        (
            row
            for row in pending
            if row.content == content and row.sender_user_id == sender_user_id and sender_user_id is not None
        ),
        None,
    )
    if duplicate is not None:
        logger.info("[OpenClaw] skip duplicate pending inbox item agent={} id={}", agent.id, duplicate.id)
        if await_wake:
            await _run_inbox_wake(agent, content, duplicate.id)
        else:
            _schedule_inbox_wake(agent, content, duplicate.id)
        return duplicate
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
    if await_wake:
        await _run_inbox_wake(agent, content, created.id)
    else:
        _schedule_inbox_wake(agent, content, created.id)
    return created


async def _run_inbox_wake(agent: AgentRecord, content: str, message_id: UUID | None = None) -> None:
    try:
        from app.services.openclaw_inbox import wake_openclaw_inbox

        woke = await wake_openclaw_inbox(agent, content=content, message_id=message_id)
        if not woke:
            logger.info("[OpenClaw] inbox left pending for {}; guest gateway will pick it up", agent.id)
            return
        if message_id is not None:
            _ = await gateway_message_dao.mark_delivered(message_id, agent.id)
    except Exception as exc:
        logger.warning("[OpenClaw] inbox wake error for {}: {}", agent.id, exc)


_wake_tasks: set[asyncio.Task[None]] = set()
_wake_running: set[UUID] = set()
_wake_again: dict[UUID, tuple[str, UUID | None]] = {}


def _schedule_inbox_wake(
    agent: AgentRecord, content: str, message_id: UUID | None = None
) -> None:
    """Start inbox wake without blocking the chat socket."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if agent.id in _wake_running:
        _wake_again[agent.id] = (content, message_id)
        return

    async def _run() -> None:
        _wake_running.add(agent.id)
        try:
            await _run_inbox_wake(agent, content, message_id)
            follow = _wake_again.pop(agent.id, None)
            if follow is not None:
                await _run_inbox_wake(agent, follow[0], follow[1])
        finally:
            _wake_running.discard(agent.id)

    task = loop.create_task(_run())
    _wake_tasks.add(task)
    task.add_done_callback(_wake_tasks.discard)
