"""Turn model selection: pick the model that answers a conversational turn.

An agent has exactly two model slots. The primary answers the turn; the fallback is a failure lane that answers only
when the primary is not usable or its provider's circuit is open (``provider_health``), and otherwise takes over after
a retryable primary failure.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.logging import logger
from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
from app.services.llm import provider_health
from app.services.llm.turn import ModelBundle, ModelSlot


@dataclass(slots=True)
class TurnSelection:
    """Result of preflight routing for one inbound user turn."""

    model: LLMModelRecord | None
    slot: ModelSlot
    reason: str
    failover_model: LLMModelRecord | None = None


def _usable(model: LLMModelRecord | None) -> LLMModelRecord | None:
    if model is None:
        return None
    if getattr(model, "enabled", True) is False:
        return None
    return model


async def load_agent_model_bundle(
    agent: AgentRecord,
    *,
    primary: LLMModelRecord | None = None,
    fallback: LLMModelRecord | None = None,
) -> ModelBundle:
    """Load any missing assigned models for ``agent``."""
    from app.dao.llm_dao import llm_model_dao

    missing_ids = [
        mid
        for mid, loaded in (
            (getattr(agent, "primary_model_id", None), primary),
            (getattr(agent, "fallback_model_id", None), fallback),
        )
        if mid and loaded is None
    ]
    loaded: dict[uuid.UUID, LLMModelRecord] = {}
    if missing_ids:
        loaded = {row.id: row for row in await llm_model_dao.get_many(missing_ids)}
    if primary is None and getattr(agent, "primary_model_id", None) and agent.primary_model_id:
        primary = loaded.get(agent.primary_model_id)
    if fallback is None and getattr(agent, "fallback_model_id", None) and agent.fallback_model_id:
        fallback = loaded.get(agent.fallback_model_id)
    from app.services.enterprise_llm import owned_model_or_none

    tenant_id = getattr(agent, "tenant_id", None)
    return ModelBundle(
        primary=owned_model_or_none(primary, tenant_id),
        fallback=owned_model_or_none(fallback, tenant_id),
    )


def _failover_for(*, selected: LLMModelRecord | None, bundle: ModelBundle) -> LLMModelRecord | None:
    fallback = _usable(bundle.fallback)
    if fallback is not None and provider_health.model_key(fallback) != provider_health.model_key(selected):
        return fallback
    return None


def _standby_for_open_primary(
    primary: LLMModelRecord | None, slot: ModelSlot, bundle: ModelBundle
) -> LLMModelRecord | None:
    """The fallback to answer instead of a primary whose provider is open; None unless it is healthier."""
    if slot != "primary" or primary is None or not provider_health.is_unavailable(primary):
        return None
    fallback = _failover_for(selected=primary, bundle=bundle)
    if fallback is None or provider_health.is_unavailable(fallback):
        return None
    return fallback


def _selection(model: LLMModelRecord | None, slot: ModelSlot, reason: str, bundle: ModelBundle) -> TurnSelection:
    return TurnSelection(
        model=model,
        slot=slot,
        reason=reason,
        failover_model=_failover_for(selected=model, bundle=bundle),
    )


def _default_worker(bundle: ModelBundle) -> tuple[LLMModelRecord | None, ModelSlot]:
    primary = _usable(bundle.primary)
    if primary is not None:
        return primary, "primary"
    fallback = _usable(bundle.fallback)
    if fallback is not None:
        return fallback, "fallback"
    return None, "primary"


def select_turn_model(bundle: ModelBundle, *, agent_id: uuid.UUID | None = None) -> TurnSelection:
    """Pick the model for one inbound user turn.

    The primary answers; the fallback does when the primary is unusable or its provider is open and the fallback's is
    not. A primary whose circuit is half-open is chosen again, so the next call can be the probe.
    """
    model, slot = _default_worker(bundle)
    reason = "primary" if slot == "primary" and model is not None else "primary_unavailable"
    standby = _standby_for_open_primary(model, slot, bundle)
    if standby is not None:
        model, slot, reason = standby, "fallback", "primary_unhealthy"
    selected = _selection(model, slot, reason, bundle)
    logger.info("[LLM route] agent={} slot={} reason={}", agent_id, selected.slot, selected.reason)
    return selected
