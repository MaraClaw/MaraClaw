"""Complexity routing for Docker-managed OpenClaw guests.

The guest LLM is whatever ``openclaw.json`` currently names. Classify in the
engine (``select_turn_model``), persist the choice on the
gateway message, then rewrite the guest primary to the fail-closed applied
model so the next heartbeat runs on that model. Fallback stays failover-only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from app.core.logging import logger
from app.dao.gateway_message_dao import gateway_message_dao
from app.db.session import connection_ctx, get_connection
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.llm import LLMModelRecord
from app.services.agent_manager import agent_manager, guest_model_ref
from app.services.agent_setup_lock import WorkspaceLockTimeoutError
from app.services.enterprise_llm import ensure_agent_company_models
from app.services.llm.router import load_agent_model_bundle, select_turn_model
from app.services.llm.turn import Complexity, ModelBundle, ModelSlot
from app.services.llm.types import OpenAIMessage
from app.services.openclaw_enqueue import insert_prepared_message as _insert_prepared_message
from app.services.openclaw_hot_cache import (
    get_cached_bundle,
    mark_ensured,
    recently_ensured,
    set_cached_bundle,
)
from app.services.openclaw_update_exclusion import guest_write_section
from app.services.openclaw_update_gate import OpenClawUpdateGateError, assert_work_allowed
from app.services.shared_chat import shared_session_id


def _bundle_usable(bundle: ModelBundle | None) -> bool:
    return bundle is not None and (
        bundle.primary is not None or bundle.secondary is not None or bundle.fallback is not None
    )


class NoCompanyModelError(RuntimeError):
    """The tenant pool has no usable model for this OpenClaw guest."""


class GuestConfigWriteError(RuntimeError):
    """Turn preparation failed before the guest configuration was ready."""


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
                if (message.selected_slot or "primary") == "primary" and message.guest_model_ref not in (None, "")
            ),
            None,
        )
        return ref, "primary"
    ref = next((message.guest_model_ref for message in messages if message.guest_model_ref not in (None, "")), None)
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


@dataclass(frozen=True, slots=True)
class PreparedOpenClawMessage:
    """Ensured destination and routing metadata, ready for a queue-only transaction."""

    agent: AgentRecord
    content: str
    selected_slot: ModelSlot
    guest_model_ref: str | None
    complexity: Complexity | None
    routing_reason: str


async def prepare_openclaw_message(
    *,
    agent: AgentRecord,
    content: str,
    history: list[OpenAIMessage] | None = None,
) -> PreparedOpenClawMessage:
    """Ensure models and rewrite guest config before queue insertion.

    Raises ``OpenClawUpdateGateError`` before any config or model side effect.
    """
    await assert_work_allowed(agent.id)
    if not recently_ensured(agent):
        agent = await ensure_agent_company_models(agent)
    bundle = get_cached_bundle(agent)
    if bundle is None:
        bundle = await load_agent_model_bundle(agent)
    if not _bundle_usable(bundle):
        raise NoCompanyModelError(
            "This company has no model assigned. Connect a Grok or ChatGPT subscription or add a model in Admin → Models, "
            + "then set it as primary."
        )
    mark_ensured(agent)
    set_cached_bundle(agent, bundle)
    choice = await select_turn_model(
        bundle,
        user_text=content,
        history=history,
        agent_id=agent.id,
        skip_classifier=True,
    )
    pending = await gateway_message_dao.list_pending(agent.id)
    apply_model = applied_guest_model(choice.slot, choice.model, bundle, pending)
    try:
        async with guest_write_section(agent.id):
            try:
                _ = agent_manager.write_guest_config(
                    agent,
                    primary=bundle.primary,
                    secondary=bundle.secondary,
                    fallback=bundle.fallback,
                    selected=apply_model,
                )
            except Exception as exc:
                raise GuestConfigWriteError("Guest configuration could not be updated. Please try again.") from exc
    except WorkspaceLockTimeoutError as exc:
        raise GuestConfigWriteError("Guest configuration could not be updated. Please try again.") from exc
    return PreparedOpenClawMessage(
        agent, content, choice.slot, guest_model_ref(choice.model), choice.complexity, choice.reason
    )


async def enqueue_prepared_openclaw_message(
    prepared: PreparedOpenClawMessage,
    *,
    sender_user_id: UUID | None = None,
    sender_agent_id: UUID | None = None,
    conversation_id: str | None = None,
    deduplicate: bool = True,
    reply_session_id: UUID | None = None,
) -> GatewayMessageRecord:
    """Queue only; wake in a fresh context after the owning transaction commits.

    The row and caller's other writes share the owning transaction;
    rollback drops the deferred wake.
    """
    async with connection_ctx():
        created = await _insert_prepared_message(
            prepared,
            sender_user_id=sender_user_id,
            sender_agent_id=sender_agent_id,
            conversation_id=conversation_id,
            deduplicate=deduplicate,
            reply_session_id=reply_session_id,
        )
        _schedule_inbox_wake(prepared.agent, prepared.content, created.id)
    return created


async def enqueue_openclaw_message(
    *,
    agent: AgentRecord,
    content: str,
    sender_user_id: UUID | None = None,
    sender_agent_id: UUID | None = None,
    conversation_id: str | None = None,
    history: list[OpenAIMessage] | None = None,
    await_wake: bool = True,
    reply_session_id: UUID | None = None,
) -> GatewayMessageRecord:
    """Prepare and queue a normal turn; optionally wait for its guest wake."""
    prepared = await prepare_openclaw_message(agent=agent, content=content, history=history)
    async with connection_ctx():
        created = await _insert_prepared_message(
            prepared,
            sender_user_id=sender_user_id,
            sender_agent_id=sender_agent_id,
            conversation_id=conversation_id,
            reply_session_id=reply_session_id,
        )
    if await_wake and get_connection() is None:
        await _run_inbox_wake(prepared.agent, content, created.id)
    else:
        _schedule_inbox_wake(prepared.agent, content, created.id)
    return created


async def _run_inbox_wake(agent: AgentRecord, content: str, message_id: UUID | None = None) -> None:
    try:
        await assert_work_allowed(agent.id)
    except OpenClawUpdateGateError as gate:
        logger.info("[OpenClaw] inbox wake left pending for {}: {}", agent.id, gate.code)
        return
    claimed: GatewayMessageRecord | None = None
    try:
        from app.services.openclaw_inbox import wake_openclaw_inbox

        if message_id is not None:
            async with connection_ctx():
                requested = await gateway_message_dao.get_for_agent(message_id, agent.id)
                if requested is not None and requested.conversation_id == str(shared_session_id(agent.id)):
                    from datetime import UTC, datetime

                    claimed = await gateway_message_dao.claim_shared(
                        agent.id, shared_session_id(agent.id), now=datetime.now(UTC)
                    )
                else:
                    claimed = await gateway_message_dao.mark_delivered(message_id, agent.id)
            if claimed is None:
                logger.info("[OpenClaw] inbox message {} already taken or completed for {}", message_id, agent.id)
                return
            content, message_id = claimed.content, claimed.id
        if claimed is not None and claimed.conversation_id == str(shared_session_id(agent.id)):
            from app.services.openclaw_turns import dispatch_shared_turn

            woke = await dispatch_shared_turn(agent, claimed)
        else:
            woke = await wake_openclaw_inbox(agent, content=content, message_id=message_id)
    except Exception as exc:
        logger.warning("[OpenClaw] inbox wake error for {}: {}", agent.id, exc)
        await _release_wake_claim(agent, claimed)
        return
    if not woke:
        await _release_wake_claim(agent, claimed)
        logger.info("[OpenClaw] inbox left pending for {}; guest gateway will pick it up", agent.id)


async def _release_wake_claim(agent: AgentRecord, claimed: GatewayMessageRecord | None) -> None:
    if claimed is None or claimed.conversation_id == str(shared_session_id(agent.id)):
        return
    try:
        _ = await gateway_message_dao.release_delivery(claimed)
    except Exception as exc:
        logger.warning("[OpenClaw] inbox claim release error for {}: {}", agent.id, exc)


_wake_tasks: set[asyncio.Task[None]] = set()
_wake_running: set[UUID] = set()
_wake_again: dict[UUID, tuple[str, UUID | None]] = {}


def _schedule_inbox_wake(agent: AgentRecord, content: str, message_id: UUID | None = None) -> None:
    """Start inbox wake without blocking the chat socket."""
    if get_connection() is not None:
        from app.api.background_tasks import schedule_background_task

        async def after_commit() -> None:
            _schedule_inbox_wake(agent, content, message_id)

        schedule_background_task(after_commit(), description="wake committed OpenClaw inbox")
        return
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
            while (follow := _wake_again.pop(agent.id, None)) is not None:
                await _run_inbox_wake(agent, follow[0], follow[1])
        finally:
            _wake_running.discard(agent.id)

    task = loop.create_task(_run())
    _wake_tasks.add(task)
    task.add_done_callback(_wake_tasks.discard)
