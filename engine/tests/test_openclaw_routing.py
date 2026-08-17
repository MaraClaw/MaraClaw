"""OpenClaw guest complexity routing: classify, fail-closed apply, poll hint."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.records.gateway_message import GatewayMessageRecord
from app.services import agent_manager as agent_manager_module, openclaw_routing
from app.services.agent_manager import AgentManager, guest_model_ref
from app.services.llm.turn import ModelBundle


def _model(*, name: str, provider: str = "anthropic", key: str = "sk-test") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider=provider,
        model=name,
        enabled=True,
        supports_vision=False,
        api_key_encrypted=key,
        label=name,
    )


def _message(*, slot: str | None, ref: str | None = None) -> GatewayMessageRecord:
    return GatewayMessageRecord(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        content="x",
        selected_slot=slot,
        guest_model_ref=ref,
    )


def test_guest_model_ref_requires_both_parts() -> None:
    assert guest_model_ref(None) is None
    assert guest_model_ref(SimpleNamespace(provider="openai", model="")) is None
    assert guest_model_ref(SimpleNamespace(provider="openai", model="gpt-5.4")) == "openai/gpt-5.4"
    assert guest_model_ref(SimpleNamespace(provider="grok", model="grok-4.6")) == "xai/grok-4.6"
    assert guest_model_ref(SimpleNamespace(provider="xai", model="auto")) == "xai/auto"


def test_requires_primary_treats_missing_slot_as_primary() -> None:
    assert openclaw_routing.requires_primary([]) is False
    assert openclaw_routing.requires_primary([_message(slot="secondary")]) is False
    assert openclaw_routing.requires_primary([_message(slot=None)]) is True
    assert openclaw_routing.requires_primary([_message(slot="secondary"), _message(slot="primary")]) is True


def test_poll_model_hint_fail_closes_mixed_batch() -> None:
    primary = _message(slot="primary", ref="anthropic/opus")
    secondary = _message(slot="secondary", ref="anthropic/haiku")
    assert openclaw_routing.poll_model_hint([]) == (None, None)
    assert openclaw_routing.poll_model_hint([secondary, primary]) == ("anthropic/opus", "primary")
    assert openclaw_routing.poll_model_hint([secondary]) == ("anthropic/haiku", "secondary")
    assert openclaw_routing.poll_model_hint([_message(slot="primary")]) == (None, "primary")


def test_applied_guest_model_does_not_downgrade_when_primary_pending() -> None:
    primary = _model(name="opus")
    secondary = _model(name="haiku")
    bundle = ModelBundle(primary=primary, secondary=secondary)
    pending = [_message(slot="primary", ref="anthropic/opus")]
    assert openclaw_routing.applied_guest_model("secondary", secondary, bundle, pending) is primary
    assert openclaw_routing.applied_guest_model("secondary", secondary, bundle, []) is secondary
    assert openclaw_routing.applied_guest_model("primary", primary, bundle, pending) is primary


def test_generate_openclaw_config_registers_secondary_and_selects_it() -> None:
    manager = AgentManager.__new__(AgentManager)
    agent = SimpleNamespace(id=uuid.uuid4(), name="Guest")
    primary = _model(name="opus", provider="anthropic", key="sk-opus")
    secondary = _model(name="haiku", provider="anthropic", key="sk-haiku")
    fallback = _model(name="gpt-5.4", provider="openai", key="sk-gpt")

    config = manager._generate_openclaw_config(
        agent,
        primary,
        secondary=secondary,
        fallback=fallback,
        selected=secondary,
    )

    assert "agent" not in config
    defaults = config["agents"]["defaults"]
    assert defaults["model"]["primary"] == "anthropic/haiku"
    assert defaults["model"]["fallbacks"] == ["openai/gpt-5.4"]
    assert defaults["models"]["anthropic/opus"] == {"alias": "primary"}
    assert defaults["models"]["anthropic/haiku"] == {"alias": "secondary"}
    assert defaults["models"]["openai/gpt-5.4"] == {"alias": "fallback"}
    assert config["env"]["vars"]["ANTHROPIC_API_KEY"] == "sk-opus"
    assert config["env"]["vars"]["OPENAI_API_KEY"] == "sk-gpt"


def test_write_guest_config_skips_missing_dir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = AgentManager.__new__(AgentManager)
    missing = tmp_path / "nope"
    monkeypatch.setattr(manager, "_agent_dir", lambda _agent_id: missing)
    agent = SimpleNamespace(id=uuid.uuid4())
    assert manager.write_guest_config(agent, primary=None) is None


def test_write_guest_config_skips_unchanged_fingerprint(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent_manager_module._guest_config_fingerprints.clear()
    manager = AgentManager.__new__(AgentManager)
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    monkeypatch.setattr(manager, "_agent_dir", lambda _agent_id: agent_dir)
    agent = SimpleNamespace(id=agent_id)
    primary = _model(name="opus")
    path = manager.write_guest_config(agent, primary=primary, selected=primary)
    assert path is not None
    path.write_text("sentinel-unchanged")
    again = manager.write_guest_config(agent, primary=primary, selected=primary)
    assert again == path
    assert path.read_text() == "sentinel-unchanged"


def test_write_guest_config_rewrites_selected_model(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = AgentManager.__new__(AgentManager)
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    monkeypatch.setattr(manager, "_agent_dir", lambda _agent_id: agent_dir)
    agent = SimpleNamespace(id=agent_id)
    primary = _model(name="opus")
    secondary = _model(name="haiku")

    path = manager.write_guest_config(agent, primary=primary, secondary=secondary, selected=secondary)
    assert path is not None
    written = path.read_text(encoding="utf-8")
    assert "anthropic/haiku" in written
    assert '"alias": "secondary"' in written


@pytest.mark.asyncio
async def test_enqueue_hi_selects_secondary_and_writes_guest_config(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _model(name="opus")
    secondary = _model(name="haiku")
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        primary_model_id=primary.id,
        secondary_model_id=secondary.id,
        fallback_model_id=None,
    )
    created_payloads: list[dict[str, object]] = []

    async def fake_load(loaded_agent, **_kwargs):
        assert loaded_agent is agent
        return ModelBundle(primary=primary, secondary=secondary)

    async def fake_pending(_agent_id):
        return []

    async def fake_create(*, obj_in):
        created_payloads.append(dict(obj_in))
        return GatewayMessageRecord(
            id=uuid.uuid4(),
            agent_id=agent.id,
            content=str(obj_in["content"]),
            selected_slot=str(obj_in.get("selected_slot") or ""),
            guest_model_ref=str(obj_in.get("guest_model_ref") or "") or None,
        )

    writes: list[object] = []

    def fake_write(loaded_agent, **kwargs):
        writes.append(kwargs)
        return tmp_path / "openclaw.json"

    monkeypatch.setattr(openclaw_routing, "load_agent_model_bundle", fake_load)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "list_pending", fake_pending)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "create", fake_create)
    monkeypatch.setattr(openclaw_routing.agent_manager, "write_guest_config", fake_write)

    row = await openclaw_routing.enqueue_openclaw_message(agent=agent, content="hi")

    assert row.selected_slot == "secondary"
    assert created_payloads[0]["guest_model_ref"] == "anthropic/haiku"
    assert created_payloads[0]["routing_reason"] == "heuristic_manageable"
    assert writes[0]["selected"] is secondary


@pytest.mark.asyncio
async def test_enqueue_does_not_downgrade_guest_when_primary_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _model(name="opus")
    secondary = _model(name="haiku")
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        primary_model_id=primary.id,
        secondary_model_id=secondary.id,
        fallback_model_id=None,
    )
    writes: list[object] = []

    async def fake_load(_agent, **_kwargs):
        return ModelBundle(primary=primary, secondary=secondary)

    async def fake_pending(_agent_id):
        return [_message(slot="primary", ref="anthropic/opus")]

    async def fake_create(*, obj_in):
        return GatewayMessageRecord(
            id=uuid.uuid4(),
            agent_id=agent.id,
            content=str(obj_in["content"]),
            selected_slot=str(obj_in.get("selected_slot") or ""),
            guest_model_ref=str(obj_in.get("guest_model_ref") or "") or None,
        )

    def fake_write(_agent, **kwargs):
        writes.append(kwargs)
        return

    monkeypatch.setattr(openclaw_routing, "load_agent_model_bundle", fake_load)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "list_pending", fake_pending)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "create", fake_create)
    monkeypatch.setattr(openclaw_routing.agent_manager, "write_guest_config", fake_write)

    row = await openclaw_routing.enqueue_openclaw_message(agent=agent, content="hi")

    assert row.selected_slot == "secondary"
    assert writes[0]["selected"] is primary


@pytest.mark.asyncio
async def test_enqueue_raises_when_company_has_no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), primary_model_id=None)

    async def fake_ensure(loaded):
        return loaded

    async def fake_load(_agent, **_kwargs):
        return ModelBundle(primary=None, secondary=None, fallback=None)

    monkeypatch.setattr(openclaw_routing, "ensure_agent_company_models", fake_ensure)
    monkeypatch.setattr(openclaw_routing, "load_agent_model_bundle", fake_load)

    with pytest.raises(openclaw_routing.NoCompanyModelError):
        await openclaw_routing.enqueue_openclaw_message(agent=agent, content="Hello Grok")


@pytest.mark.asyncio
async def test_enqueue_skips_llm_classifier_for_ambiguous_text(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _model(name="opus")
    secondary = _model(name="haiku")
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        primary_model_id=primary.id,
        secondary_model_id=secondary.id,
        fallback_model_id=None,
    )
    classifier_calls: list[object] = []

    async def fake_load(_agent, **_kwargs):
        return ModelBundle(primary=primary, secondary=secondary)

    async def fake_pending(_agent_id):
        return []

    async def fake_create(*, obj_in):
        return GatewayMessageRecord(
            id=uuid.uuid4(),
            agent_id=agent.id,
            content=str(obj_in["content"]),
            selected_slot=str(obj_in.get("selected_slot") or ""),
            guest_model_ref=str(obj_in.get("guest_model_ref") or "") or None,
            routing_reason=str(obj_in.get("routing_reason") or "") or None,
        )

    async def boom(*_args, **_kwargs):
        classifier_calls.append(1)
        raise AssertionError("classifier should not run on OpenClaw enqueue")

    monkeypatch.setattr(openclaw_routing, "load_agent_model_bundle", fake_load)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "list_pending", fake_pending)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "create", fake_create)
    monkeypatch.setattr(openclaw_routing.agent_manager, "write_guest_config", lambda *_a, **_k: None)
    monkeypatch.setattr("app.services.llm.router._classify_with_llm", boom)

    row = await openclaw_routing.enqueue_openclaw_message(
        agent=agent,
        content="Can you help me with this tomorrow morning please",
        await_wake=False,
    )
    assert classifier_calls == []
    assert row.selected_slot == "primary"
    assert row.routing_reason == "heuristic_unknown"


@pytest.mark.asyncio
async def test_enqueue_reuses_cached_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import openclaw_hot_cache

    openclaw_hot_cache.reset()
    primary = _model(name="opus")
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        primary_model_id=primary.id,
        secondary_model_id=None,
        fallback_model_id=None,
        tenant_id=None,
    )
    loads: list[int] = []

    async def fake_ensure(loaded):
        return loaded

    async def fake_load(_agent, **_kwargs):
        loads.append(1)
        return ModelBundle(primary=primary)

    async def fake_pending(_agent_id):
        return []

    created = 0

    async def fake_create(*, obj_in):
        nonlocal created
        created += 1
        return GatewayMessageRecord(
            id=uuid.uuid4(),
            agent_id=agent.id,
            content=str(obj_in["content"]) + str(created),
        )

    monkeypatch.setattr(openclaw_routing, "ensure_agent_company_models", fake_ensure)
    monkeypatch.setattr(openclaw_routing, "load_agent_model_bundle", fake_load)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "list_pending", fake_pending)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "create", fake_create)
    monkeypatch.setattr(openclaw_routing.agent_manager, "write_guest_config", lambda *_a, **_k: None)

    _ = await openclaw_routing.enqueue_openclaw_message(agent=agent, content="one", await_wake=False)
    _ = await openclaw_routing.enqueue_openclaw_message(agent=agent, content="two", await_wake=False)
    assert loads == [1]


@pytest.mark.asyncio
async def test_enqueue_skips_duplicate_pending_from_same_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _model(name="opus")
    user_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        primary_model_id=primary.id,
        secondary_model_id=None,
        fallback_model_id=None,
    )
    existing = GatewayMessageRecord(
        id=uuid.uuid4(),
        agent_id=agent.id,
        content="Please begin the onboarding.",
        sender_user_id=user_id,
        status="pending",
    )
    created: list[object] = []
    wakes: list[object] = []

    async def fake_load(_agent, **_kwargs):
        return ModelBundle(primary=primary)

    async def fake_pending(_agent_id):
        return [existing]

    async def fake_create(*, obj_in):
        created.append(obj_in)
        return existing

    async def fake_wake(_agent, _content, _message_id=None):
        wakes.append(_message_id)

    monkeypatch.setattr(openclaw_routing, "load_agent_model_bundle", fake_load)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "list_pending", fake_pending)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "create", fake_create)
    monkeypatch.setattr(openclaw_routing.agent_manager, "write_guest_config", lambda *_a, **_k: None)
    monkeypatch.setattr(openclaw_routing, "_run_inbox_wake", fake_wake)

    row = await openclaw_routing.enqueue_openclaw_message(
        agent=agent,
        content="Please begin the onboarding.",
        sender_user_id=user_id,
        await_wake=True,
    )
    assert row.id == existing.id
    assert created == []
    assert wakes == [existing.id]


@pytest.mark.asyncio
async def test_successful_wake_marks_inbox_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = SimpleNamespace(id=uuid.uuid4())
    message_id = uuid.uuid4()
    marked: list[tuple[object, object]] = []

    async def fake_wake(_agent, *, content, message_id):
        del content, message_id
        return True

    async def fake_mark(mid, agent_id):
        marked.append((mid, agent_id))

    monkeypatch.setattr("app.services.openclaw_inbox.wake_openclaw_inbox", fake_wake)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "mark_delivered", fake_mark)

    await openclaw_routing._run_inbox_wake(agent, "hello", message_id)
    assert marked == [(message_id, agent.id)]
