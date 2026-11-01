"""Read-only OpenClaw model selection.

Shared producers choose routing metadata here without ensuring company models, filling caches, rewriting the guest
configuration or touching Docker/queues. The guest configuration is applied only when a Shared turn is claimed
(``openclaw_shared_dispatch``), so admission can never overwrite the model of a turn that is already running.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.llm import LLMModelRecord
from app.records.openclaw_dispatch import PreparedOpenClawMessage
from app.services.agent_manager import guest_model_ref
from app.services.agent_work_eligibility import load_agent_for_new_work
from app.services.llm.router import load_agent_model_bundle, select_turn_model
from app.services.llm.turn import Complexity, ModelBundle, ModelSlot
from app.services.llm.types import OpenAIMessage
from app.services.openclaw_hot_cache import get_cached_bundle
from app.services.openclaw_update_gate import assert_work_allowed


class NoCompanyModelError(RuntimeError):
    """The tenant pool has no usable model for this OpenClaw guest."""


def bundle_usable(bundle: ModelBundle | None) -> bool:
    return bundle is not None and (
        bundle.primary is not None or bundle.secondary is not None or bundle.fallback is not None
    )


@dataclass(frozen=True, slots=True)
class RouteChoice:
    """One routing decision together with the bundle it was resolved against."""

    bundle: ModelBundle
    model: LLMModelRecord | None
    slot: ModelSlot
    complexity: Complexity | None
    reason: str

    @property
    def ref(self) -> str | None:
        return guest_model_ref(self.model)


async def choose_route(
    agent: AgentRecord, content: str, *, history: list[OpenAIMessage] | None = None, fresh: bool = False
) -> RouteChoice:
    """Select the model for ``content``; ``fresh`` skips the process-local bundle cache (dispatch time)."""
    bundle = None if fresh else get_cached_bundle(agent)
    if bundle is None:
        bundle = await load_agent_model_bundle(agent)
    if not bundle_usable(bundle):
        raise NoCompanyModelError(
            "This company has no model assigned. Connect a Grok or ChatGPT subscription or add a model in Admin → Models, "
            "then set it as primary."
        )
    choice = await select_turn_model(
        bundle, user_text=content, history=history, agent_id=agent.id, skip_classifier=True
    )
    return RouteChoice(bundle, choice.model, choice.slot, choice.complexity, choice.reason)


async def prepare_shared_openclaw_message(
    *, agent: AgentRecord, content: str, history: list[OpenAIMessage] | None = None
) -> PreparedOpenClawMessage:
    """Eligibility-checked routing metadata for a Shared-lane entry; nothing is written anywhere.

    Raises ``OpenClawUpdateGateError`` before any selection, like the nonshared preparation.
    """
    await assert_work_allowed(agent.id)
    agent = await load_agent_for_new_work(agent.id)
    choice = await choose_route(agent, content, history=history)
    return PreparedOpenClawMessage(agent, content, choice.slot, choice.ref, choice.complexity, choice.reason)


def find_stored_model(bundle: ModelBundle, ref: str) -> LLMModelRecord | None:
    """The eligible assigned model with exactly this guest ref; never a substitute."""
    for model in (bundle.primary, bundle.secondary, bundle.fallback):
        if model is not None and guest_model_ref(model) == ref:
            return model
    return None


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """The bundle a stored selection was resolved against, and the exact model it names."""

    bundle: ModelBundle
    model: LLMModelRecord | None


async def resolve_stored_model(agent: AgentRecord, ref: str) -> ResolvedModel | None:
    """Resolve a row's stored ``provider/model`` against a FRESH eligible bundle; ``None`` when it is unavailable."""
    bundle = await load_agent_model_bundle(agent)
    model = find_stored_model(bundle, ref)
    return ResolvedModel(bundle, model) if model is not None else None


def requires_primary(messages: Sequence[GatewayMessageRecord]) -> bool:
    """True when any queued row is missing a slot or is classified primary."""
    return any((message.selected_slot or "primary") == "primary" for message in messages)


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
