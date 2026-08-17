"""Complexity preflight: heuristic, parse, fail-closed, slot selection."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.llm import router
from app.services.llm.turn import ModelBundle


def _model(*, enabled: bool = True, vision: bool = False, name: str = "m") -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), model=name, enabled=enabled, supports_vision=vision, provider="grok")


def test_parse_complexity_label_accepts_json_and_raw_words() -> None:
    assert router.parse_complexity_label('{"complexity":"manageable"}') == "manageable"
    assert router.parse_complexity_label('Here you go {"complexity": "complex"}') == "complex"
    assert router.parse_complexity_label("manageable") == "manageable"
    assert router.parse_complexity_label("COMPLEX") == "complex"
    assert router.parse_complexity_label("maybe later") is None
    assert router.parse_complexity_label("") is None


def test_heuristic_force_complex() -> None:
    assert router.heuristic_complexity("x" * 1201) == "complex"
    assert router.heuristic_complexity("Please design the migration") == "complex"
    assert router.heuristic_complexity("investigate the root cause") == "complex"
    assert router.heuristic_complexity("look in the repo") == "complex"
    history = [{"role": "assistant", "tool_calls": [{"id": "1"}]}]
    assert router.heuristic_complexity("ok next", history=history) == "complex"
    assert router.heuristic_complexity("What is the status? And the owner?\n1. first\n2. second") == "complex"


def test_heuristic_force_manageable() -> None:
    assert router.heuristic_complexity("hi") == "manageable"
    assert router.heuristic_complexity("Thanks") == "manageable"
    assert router.heuristic_complexity("what time is it?") == "manageable"
    assert router.heuristic_complexity("ok") == "manageable"
    assert router.heuristic_complexity("Is the office open?") == "manageable"


def test_heuristic_ambiguous_returns_none() -> None:
    assert router.heuristic_complexity("Can you help me with this tomorrow morning please") is None
    assert router.heuristic_complexity("see attached", has_images=True) is None


def test_turn_has_images() -> None:
    assert router.turn_has_images("hello [image_data:data:image/png;base64,abc]", None) is True
    assert router.turn_has_images("hello", [{"role": "user", "content": [{"type": "image_url"}]}]) is False
    assert router.turn_has_images("hello", None) is False


def test_history_without_current_user_drops_duplicate() -> None:
    current = "do the second one"
    history = [
        {"role": "user", "content": "compare A and B"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": current},
    ]
    prior = router.history_without_current_user(history, current)
    assert [msg["content"] for msg in prior] == ["compare A and B", "ok"]


def test_classifier_payload_skips_appended_current_and_strips_images() -> None:
    current = "what is this"
    history = [
        {"role": "user", "content": "earlier [image_data:data:image/png;base64,AAA]"},
        {"role": "user", "content": current},
    ]
    payload = router.classifier_user_payload(current, history)
    assert "earlier" in payload
    assert payload.count("what is this") == 1
    assert "AAA" not in payload
    assert "[image attached]" in payload


@pytest.mark.asyncio
async def test_select_skips_classifier_when_no_secondary() -> None:
    primary = _model(name="primary")
    fallback = _model(name="fallback")
    choice = await router.select_turn_model(
        ModelBundle(primary=primary, fallback=fallback),
        user_text="hi",
    )
    assert choice.reason == "no_secondary"
    assert choice.model is primary
    assert choice.failover_model is fallback
    assert choice.slot == "primary"


@pytest.mark.asyncio
async def test_select_greeting_prefers_secondary() -> None:
    primary = _model(name="primary")
    secondary = _model(name="secondary")
    choice = await router.select_turn_model(
        ModelBundle(primary=primary, secondary=secondary),
        user_text="Please begin the onboarding.",
        skip_tools=True,
    )
    assert choice.reason == "greeting"
    assert choice.slot == "secondary"
    assert choice.model is secondary


@pytest.mark.asyncio
async def test_select_vision_uses_secondary_when_only_it_has_vision() -> None:
    primary = _model(name="primary", vision=False)
    secondary = _model(name="secondary", vision=True)
    choice = await router.select_turn_model(
        ModelBundle(primary=primary, secondary=secondary),
        user_text="what is this [image_data:data:image/png;base64,xx]",
    )
    assert choice.reason == "vision"
    assert choice.model is secondary


@pytest.mark.asyncio
async def test_select_vision_escalates_to_primary() -> None:
    primary = _model(name="primary", vision=True)
    secondary = _model(name="secondary", vision=False)
    choice = await router.select_turn_model(
        ModelBundle(primary=primary, secondary=secondary),
        user_text="what is this [image_data:data:image/png;base64,xx]",
    )
    assert choice.reason == "vision"
    assert choice.model is primary


@pytest.mark.asyncio
async def test_select_heuristic_manageable_uses_secondary() -> None:
    primary = _model(name="primary")
    secondary = _model(name="secondary")
    choice = await router.select_turn_model(
        ModelBundle(primary=primary, secondary=secondary),
        user_text="thanks",
    )
    assert choice.reason == "heuristic_manageable"
    assert choice.model is secondary
    assert choice.complexity == "manageable"


@pytest.mark.asyncio
async def test_select_heuristic_complex_uses_primary() -> None:
    primary = _model(name="primary")
    secondary = _model(name="secondary")
    choice = await router.select_turn_model(
        ModelBundle(primary=primary, secondary=secondary),
        user_text="Please design a Q3 OKR plan for sales",
    )
    assert choice.reason == "heuristic_complex"
    assert choice.model is primary


@pytest.mark.asyncio
async def test_prior_image_does_not_lock_later_greeting() -> None:
    primary = _model(name="primary", vision=True)
    secondary = _model(name="secondary", vision=False)
    choice = await router.select_turn_model(
        ModelBundle(primary=primary, secondary=secondary),
        user_text="thanks",
        history=[{"role": "user", "content": "[image_data:data:image/png;base64,xx]"}],
    )
    assert choice.reason == "heuristic_manageable"
    assert choice.model is secondary


@pytest.mark.asyncio
async def test_classify_timeout_fail_closes_to_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _model(name="primary")
    secondary = _model(name="secondary")

    class _FakeClient:
        async def complete(self, **_kwargs: object) -> object:
            raise TimeoutError

        async def close(self) -> None:
            return None

    from app.services.llm import utils as llm_utils

    monkeypatch.setattr(llm_utils, "create_llm_client", lambda **_kwargs: _FakeClient())
    monkeypatch.setattr(llm_utils, "get_model_api_key", lambda _model: "sk-test")
    choice = await router.select_turn_model(
        ModelBundle(primary=primary, secondary=secondary),
        user_text="Can you help me with this tomorrow morning please",
    )
    assert choice.reason == "fail_closed"
    assert choice.model is primary


@pytest.mark.asyncio
async def test_select_classifier_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _model(name="primary")
    secondary = _model(name="secondary")

    async def boom(*_args: object, **_kwargs: object) -> tuple[None, int, int]:
        return None, 12, 0

    monkeypatch.setattr(router, "_classify_with_llm", boom)
    choice = await router.select_turn_model(
        ModelBundle(primary=primary, secondary=secondary),
        user_text="Can you help me with this tomorrow morning please",
    )
    assert choice.reason == "fail_closed"
    assert choice.model is primary
    assert choice.complexity == "complex"


@pytest.mark.asyncio
async def test_select_same_model_skips_classifier(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _model(name="grok-4.6")
    secondary = _model(name="grok-4.6")

    async def boom(*_args: object, **_kwargs: object) -> tuple[None, int, int]:
        raise AssertionError("classifier should not run when slots share a model")

    monkeypatch.setattr(router, "_classify_with_llm", boom)
    choice = await router.select_turn_model(
        ModelBundle(primary=primary, secondary=secondary),
        user_text="Can you help me with this tomorrow morning please",
    )
    assert choice.reason == "same_model"
    assert choice.model is primary


@pytest.mark.asyncio
async def test_select_skip_classifier_fail_closes_unknown() -> None:
    primary = _model(name="primary")
    secondary = _model(name="secondary")
    choice = await router.select_turn_model(
        ModelBundle(primary=primary, secondary=secondary),
        user_text="Can you help me with this tomorrow morning please",
        skip_classifier=True,
    )
    assert choice.reason == "heuristic_unknown"
    assert choice.model is primary
    assert choice.complexity == "complex"


@pytest.mark.asyncio
async def test_select_classifier_manageable(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _model(name="primary")
    secondary = _model(name="secondary")

    async def classify(*_args: object, **_kwargs: object) -> tuple[str, int, int]:
        return "manageable", 40, 8

    monkeypatch.setattr(router, "_classify_with_llm", classify)
    choice = await router.select_turn_model(
        ModelBundle(primary=primary, secondary=secondary),
        user_text="Can you help me with this tomorrow morning please",
    )
    assert choice.reason == "classifier"
    assert choice.model is secondary
    assert choice.classifier_tokens == 8


@pytest.mark.asyncio
async def test_secondary_without_fallback_escalates_to_primary() -> None:
    primary = _model(name="primary")
    secondary = _model(name="secondary")
    choice = await router.select_turn_model(
        ModelBundle(primary=primary, secondary=secondary),
        user_text="hi",
    )
    assert choice.model is secondary
    assert choice.failover_model is primary


@pytest.mark.asyncio
async def test_force_primary_skips_secondary() -> None:
    primary = _model(name="primary")
    secondary = _model(name="secondary")
    choice = await router.select_turn_model(
        ModelBundle(primary=primary, secondary=secondary),
        user_text="hi",
        force_primary=True,
    )
    assert choice.reason == "force_primary"
    assert choice.model is primary


@pytest.mark.asyncio
async def test_disabled_secondary_behaves_as_unset() -> None:
    primary = _model(name="primary")
    secondary = _model(name="secondary", enabled=False)
    choice = await router.select_turn_model(
        ModelBundle(primary=primary, secondary=secondary),
        user_text="hi",
    )
    assert choice.reason == "no_secondary"
    assert choice.model is primary


@pytest.mark.asyncio
async def test_load_bundle_fetches_missing_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = uuid4()
    secondary_id = uuid4()
    secondary = _model(name="secondary")
    secondary.id = secondary_id
    secondary.tenant_id = tenant_id

    async def fake_get_many(ids):
        assert list(ids) == [secondary_id]
        return [secondary]

    from app.dao.llm_dao import llm_model_dao

    monkeypatch.setattr(llm_model_dao, "get_many", fake_get_many)
    agent = SimpleNamespace(
        primary_model_id=None,
        secondary_model_id=secondary_id,
        fallback_model_id=None,
        tenant_id=tenant_id,
    )
    bundle = await router.load_agent_model_bundle(agent)
    assert bundle.secondary is secondary


@pytest.mark.asyncio
async def test_load_bundle_drops_foreign_tenant_model(monkeypatch: pytest.MonkeyPatch) -> None:
    foreign = _model(name="leak")
    foreign.tenant_id = uuid4()

    async def fake_get_many(ids):
        return [foreign]

    from app.dao.llm_dao import llm_model_dao

    monkeypatch.setattr(llm_model_dao, "get_many", fake_get_many)
    agent = SimpleNamespace(
        primary_model_id=foreign.id,
        secondary_model_id=None,
        fallback_model_id=None,
        tenant_id=uuid4(),
    )
    bundle = await router.load_agent_model_bundle(agent)
    assert bundle.primary is None
