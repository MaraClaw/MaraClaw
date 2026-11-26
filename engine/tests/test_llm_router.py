"""Turn model selection: primary first, fallback only when the primary is unusable."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from tests.llm_retry_fakes import FakeClock, install_provider_health, trip

from app.services.llm import caller, router
from app.services.llm.provider_health import Admission, ProviderHealth, model_key
from app.services.llm.turn import ModelBundle, TurnContext


def _model(*, enabled: bool = True, vision: bool = False, name: str = "m") -> Any:
    return SimpleNamespace(id=uuid4(), model=name, enabled=enabled, supports_vision=vision, provider="grok")


def test_select_uses_primary_and_fails_over_to_fallback() -> None:
    primary = _model(name="primary")
    fallback = _model(name="fallback")
    choice = router.select_turn_model(ModelBundle(primary=primary, fallback=fallback))
    assert choice.reason == "primary"
    assert choice.model is primary
    assert choice.failover_model is fallback
    assert choice.slot == "primary"


def test_select_primary_without_fallback_has_no_failover() -> None:
    primary = _model(name="primary")
    choice = router.select_turn_model(ModelBundle(primary=primary))
    assert (choice.model, choice.slot, choice.failover_model) == (primary, "primary", None)


def test_select_uses_fallback_when_primary_is_disabled() -> None:
    primary = _model(name="primary", enabled=False)
    fallback = _model(name="fallback")
    choice = router.select_turn_model(ModelBundle(primary=primary, fallback=fallback))
    assert choice.reason == "primary_unavailable"
    assert choice.model is fallback
    assert choice.slot == "fallback"
    assert choice.failover_model is None


def test_select_uses_fallback_when_primary_is_missing() -> None:
    fallback = _model(name="fallback")
    choice = router.select_turn_model(ModelBundle(fallback=fallback))
    assert (choice.model, choice.slot, choice.failover_model) == (fallback, "fallback", None)


def test_select_ignores_a_disabled_fallback_as_failover() -> None:
    primary = _model(name="primary")
    choice = router.select_turn_model(ModelBundle(primary=primary, fallback=_model(enabled=False)))
    assert choice.model is primary
    assert choice.failover_model is None


def test_select_without_any_usable_model_returns_none() -> None:
    choice = router.select_turn_model(ModelBundle(primary=_model(enabled=False), fallback=_model(enabled=False)))
    assert choice.model is None
    assert choice.failover_model is None


@pytest.fixture
def health(monkeypatch: pytest.MonkeyPatch) -> tuple[ProviderHealth, FakeClock]:
    return install_provider_health(monkeypatch)


def test_select_routes_to_the_fallback_while_the_primary_provider_is_open(
    health: tuple[ProviderHealth, FakeClock],
) -> None:
    # Given a primary whose circuit is open and a healthy fallback.
    tracker, _clock = health
    primary, fallback = _model(name="primary"), _model(name="fallback")
    trip(tracker, primary)
    # When a turn is routed.
    choice = router.select_turn_model(ModelBundle(primary=primary, fallback=fallback))
    # Then the fallback answers it and there is nothing left to fail over to.
    assert (choice.model, choice.slot, choice.reason, choice.failover_model) == (
        fallback,
        "fallback",
        "primary_unhealthy",
        None,
    )


def test_select_leaves_a_primary_below_the_failure_threshold_alone(health: tuple[ProviderHealth, FakeClock]) -> None:
    tracker, _clock = health
    primary, fallback = _model(name="primary"), _model(name="fallback")
    trip(tracker, primary, failures=2)
    choice = router.select_turn_model(ModelBundle(primary=primary, fallback=fallback))
    assert (choice.model, choice.slot, choice.reason, choice.failover_model) == (
        primary,
        "primary",
        "primary",
        fallback,
    )


def test_select_keeps_an_open_primary_when_there_is_no_fallback(health: tuple[ProviderHealth, FakeClock]) -> None:
    # Given an open primary and nothing to route to, the call itself fails fast.
    tracker, _clock = health
    primary = _model(name="primary")
    trip(tracker, primary)
    choice = router.select_turn_model(ModelBundle(primary=primary))
    assert (choice.model, choice.slot, choice.failover_model) == (primary, "primary", None)


def test_select_keeps_an_open_primary_when_the_fallback_is_open_too(health: tuple[ProviderHealth, FakeClock]) -> None:
    # Given both circuits open, switching would only trade one refusal for another.
    tracker, _clock = health
    primary, fallback = _model(name="primary"), _model(name="fallback")
    trip(tracker, primary)
    trip(tracker, fallback)
    choice = router.select_turn_model(ModelBundle(primary=primary, fallback=fallback))
    assert (choice.model, choice.reason, choice.failover_model) == (primary, "primary", fallback)


def test_select_only_follows_the_circuit_of_the_chosen_model(health: tuple[ProviderHealth, FakeClock]) -> None:
    # Given another model's circuit open, and the unusable-primary reason kept for a disabled primary.
    tracker, _clock = health
    primary, fallback, other = _model(name="primary"), _model(name="fallback"), _model(name="other")
    trip(tracker, other)
    assert router.select_turn_model(ModelBundle(primary=primary, fallback=fallback)).model is primary
    disabled = _model(name="disabled", enabled=False)
    choice = router.select_turn_model(ModelBundle(primary=disabled, fallback=fallback))
    assert (choice.model, choice.reason) == (fallback, "primary_unavailable")


def test_select_returns_to_the_primary_through_a_successful_probe(health: tuple[ProviderHealth, FakeClock]) -> None:
    # Given an open primary that has served its cooldown.
    tracker, clock = health
    primary, fallback = _model(name="primary"), _model(name="fallback")
    bundle = ModelBundle(primary=primary, fallback=fallback)
    key = model_key(primary)
    assert key is not None
    trip(tracker, primary)
    clock.advance(29.9)
    assert router.select_turn_model(bundle).model is fallback
    clock.advance(0.1)
    # When the cooldown ends, the primary is chosen again so that the next call can be the probe.
    assert router.select_turn_model(bundle).model is primary
    probe = tracker.admit(key)
    assert probe is Admission.PROBE
    # Then turns keep going to the fallback while the probe is in flight, and come back once it succeeds.
    assert router.select_turn_model(bundle).model is fallback
    assert tracker.succeeded(key, probe)
    choice = router.select_turn_model(bundle)
    assert (choice.model, choice.reason, choice.failover_model) == (primary, "primary", fallback)


def test_select_keeps_the_fallback_after_a_failed_probe(health: tuple[ProviderHealth, FakeClock]) -> None:
    tracker, clock = health
    primary, fallback = _model(name="primary"), _model(name="fallback")
    bundle = ModelBundle(primary=primary, fallback=fallback)
    key = model_key(primary)
    assert key is not None
    trip(tracker, primary)
    clock.advance(30)
    probe = tracker.admit(key)
    assert probe is Admission.PROBE
    assert tracker.failed(key, probe)
    assert router.select_turn_model(bundle).model is fallback
    clock.advance(30)
    assert router.select_turn_model(bundle).model is primary


@pytest.mark.asyncio
async def test_call_agent_llm_hands_the_failover_the_fallback_while_the_primary_is_open(
    monkeypatch: pytest.MonkeyPatch, health: tuple[ProviderHealth, FakeClock]
) -> None:
    # Given an agent whose primary circuit is open.
    tracker, _clock = health
    seen: dict[str, object] = {}

    async def fake_failover(**kwargs: object) -> str:
        seen.update(primary=kwargs["primary_model"], fallback=kwargs["fallback_model"], turn=kwargs["turn"])
        return "ok"

    monkeypatch.setattr(caller, "call_llm_with_failover", fake_failover)
    tenant_id = uuid4()
    primary, fallback = _model(name="primary"), _model(name="fallback")
    primary.tenant_id = fallback.tenant_id = tenant_id
    trip(tracker, primary)
    agent: Any = SimpleNamespace(
        id=uuid4(),
        name="Agent",
        role_description="role",
        primary_model_id=primary.id,
        fallback_model_id=fallback.id,
        tenant_id=tenant_id,
        is_expired=False,
        expires_at=None,
    )
    # When a turn arrives.
    turn = TurnContext(agent=agent, primary_model=primary, fallback_model=fallback)
    assert await caller.call_agent_llm(agent.id, "hi", turn=turn) == "ok"
    # Then the fallback answers it and the turn records why.
    assert (seen["primary"], seen["fallback"]) == (fallback, None)
    assert (turn.selected_slot, turn.routing_reason) == ("fallback", "primary_unhealthy")


@pytest.mark.asyncio
async def test_load_bundle_fetches_missing_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = uuid4()
    fallback_id = uuid4()
    fallback = _model(name="fallback")
    fallback.id = fallback_id
    fallback.tenant_id = tenant_id

    async def fake_get_many(ids):
        assert list(ids) == [fallback_id]
        return [fallback]

    from app.dao.llm_dao import llm_model_dao

    monkeypatch.setattr(llm_model_dao, "get_many", fake_get_many)
    agent: Any = SimpleNamespace(
        primary_model_id=None,
        fallback_model_id=fallback_id,
        tenant_id=tenant_id,
    )
    bundle = await router.load_agent_model_bundle(agent)
    assert bundle.fallback is fallback


@pytest.mark.asyncio
async def test_load_bundle_drops_foreign_tenant_model(monkeypatch: pytest.MonkeyPatch) -> None:
    foreign = _model(name="leak")
    foreign.tenant_id = uuid4()

    async def fake_get_many(ids):
        return [foreign]

    from app.dao.llm_dao import llm_model_dao

    monkeypatch.setattr(llm_model_dao, "get_many", fake_get_many)
    agent: Any = SimpleNamespace(
        primary_model_id=foreign.id,
        fallback_model_id=None,
        tenant_id=uuid4(),
    )
    bundle = await router.load_agent_model_bundle(agent)
    assert bundle.primary is None
