"""Turn model selection: primary first, fallback only when the primary is unusable."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.services.llm import router
from app.services.llm.turn import ModelBundle


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
