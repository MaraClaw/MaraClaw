"""OpenClaw guest complexity routing: classify, fail-closed apply, poll hint."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.config import get_settings
from app.core.security import encrypt_data
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services import agent_manager as agent_manager_module, openclaw_routing
from app.services.agent_manager import AgentManager, guest_model_ref
from app.services.llm.turn import ModelBundle


@pytest.fixture(autouse=True)
def legacy_database_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def connection() -> AsyncGenerator[None]:
        yield None

    async def get_message(message_id: uuid.UUID, agent_id: uuid.UUID) -> GatewayMessageRecord | None:
        return None

    async def open_gate(agent_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(openclaw_routing, "connection_ctx", connection)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "get_for_agent", get_message)
    monkeypatch.setattr(openclaw_routing, "assert_work_allowed", open_gate)


def _model(*, name: str, provider: str = "anthropic", key: str = "sk-test") -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider=provider,
        model=name,
        enabled=True,
        supports_vision=False,
        api_key_encrypted=encrypt_data(key, get_settings().SECRET_KEY),
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
    assert guest_model_ref(cast(Any, SimpleNamespace(provider="openai", model=""))) is None
    assert guest_model_ref(cast(Any, SimpleNamespace(provider="openai", model="gpt-5.4"))) == "openai/gpt-5.4"
    assert guest_model_ref(cast(Any, SimpleNamespace(provider="grok", model="grok-4.8"))) == "xai/grok-4.8"
    assert guest_model_ref(cast(Any, SimpleNamespace(provider="xai", model="auto"))) == "xai/auto"


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
    agent: Any = SimpleNamespace(id=uuid.uuid4(), name="Guest")
    primary = _model(name="opus", provider="anthropic", key="sk-opus")
    secondary = _model(name="haiku", provider="anthropic", key="sk-haiku")
    fallback = _model(name="gpt-5.4", provider="openai", key="sk-gpt")

    config: dict[str, Any] = manager._generate_openclaw_config(
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
    agent: Any = SimpleNamespace(id=uuid.uuid4())
    assert manager.write_guest_config(agent, primary=None) is None


def test_write_guest_config_skips_unchanged_fingerprint(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent_manager_module._guest_config_fingerprints.clear()
    manager = AgentManager.__new__(AgentManager)
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    monkeypatch.setattr(manager, "_agent_dir", lambda _agent_id: agent_dir)
    agent: Any = SimpleNamespace(id=agent_id)
    primary = _model(name="opus")
    path = manager.write_guest_config(agent, primary=primary, selected=primary)
    assert path is not None
    path.write_text("sentinel-unchanged")
    again = manager.write_guest_config(agent, primary=primary, selected=primary)
    assert again == path
    assert path.read_text() == "sentinel-unchanged"


def test_write_guest_config_rewrites_when_api_key_changes(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent_manager_module._guest_config_fingerprints.clear()
    manager = AgentManager.__new__(AgentManager)
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    monkeypatch.setattr(manager, "_agent_dir", lambda _agent_id: agent_dir)
    agent: Any = SimpleNamespace(id=agent_id)
    primary = _model(name="opus", key="sk-old")
    path = manager.write_guest_config(agent, primary=primary, selected=primary)
    assert path is not None
    rotated = _model(name="opus", key="sk-new")
    rotated.id = primary.id
    again = manager.write_guest_config(agent, primary=rotated, selected=rotated)
    assert again == path
    assert "sk-new" in path.read_text(encoding="utf-8")


def test_write_guest_config_rewrites_selected_model(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = AgentManager.__new__(AgentManager)
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    monkeypatch.setattr(manager, "_agent_dir", lambda _agent_id: agent_dir)
    agent: Any = SimpleNamespace(id=agent_id)
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
    agent: Any = SimpleNamespace(
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

    writes: list[dict[str, Any]] = []

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
    agent: Any = SimpleNamespace(
        id=uuid.uuid4(),
        primary_model_id=primary.id,
        secondary_model_id=secondary.id,
        fallback_model_id=None,
    )
    writes: list[dict[str, Any]] = []

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
    agent: Any = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), primary_model_id=None)

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
    agent: Any = SimpleNamespace(
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
    agent: Any = SimpleNamespace(
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
    agent: Any = SimpleNamespace(
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
        conversation_id="session-a",
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
        conversation_id="session-a",
        await_wake=True,
    )
    assert row.id == existing.id
    assert created == []
    assert wakes == [existing.id]


@pytest.mark.asyncio
async def test_enqueue_same_text_in_other_session_is_not_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _model(name="opus")
    user_id = uuid.uuid4()
    agent: Any = SimpleNamespace(
        id=uuid.uuid4(),
        primary_model_id=primary.id,
        secondary_model_id=None,
        fallback_model_id=None,
    )
    existing = GatewayMessageRecord(
        id=uuid.uuid4(),
        agent_id=agent.id,
        content="ok",
        sender_user_id=user_id,
        conversation_id="session-a",
        status="pending",
    )
    created: list[object] = []

    async def fake_load(_agent, **_kwargs):
        return ModelBundle(primary=primary)

    async def fake_pending(_agent_id):
        return [existing]

    async def fake_create(*, obj_in):
        created.append(obj_in)
        return GatewayMessageRecord(id=uuid.uuid4(), agent_id=agent.id, content=str(obj_in["content"]))

    monkeypatch.setattr(openclaw_routing, "load_agent_model_bundle", fake_load)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "list_pending", fake_pending)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "create", fake_create)
    monkeypatch.setattr(openclaw_routing.agent_manager, "write_guest_config", lambda *_a, **_k: None)

    row = await openclaw_routing.enqueue_openclaw_message(
        agent=agent,
        content="ok",
        sender_user_id=user_id,
        conversation_id="session-b",
        await_wake=False,
    )
    assert row.id != existing.id
    assert created


@pytest.mark.asyncio
async def test_enqueue_does_not_cache_empty_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import openclaw_hot_cache

    openclaw_hot_cache.reset()
    primary = _model(name="opus")
    agent: Any = SimpleNamespace(
        id=uuid.uuid4(),
        primary_model_id=None,
        secondary_model_id=None,
        fallback_model_id=None,
        tenant_id=uuid.uuid4(),
    )
    loads = 0

    async def fake_ensure(loaded):
        return loaded

    async def fake_load(_agent, **_kwargs):
        nonlocal loads
        loads += 1
        if loads == 1:
            return ModelBundle(primary=None)
        return ModelBundle(primary=primary)

    async def fake_pending(_agent_id):
        return []

    async def fake_create(*, obj_in):
        return GatewayMessageRecord(id=uuid.uuid4(), agent_id=agent.id, content=str(obj_in["content"]))

    monkeypatch.setattr(openclaw_routing, "ensure_agent_company_models", fake_ensure)
    monkeypatch.setattr(openclaw_routing, "load_agent_model_bundle", fake_load)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "list_pending", fake_pending)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "create", fake_create)
    monkeypatch.setattr(openclaw_routing.agent_manager, "write_guest_config", lambda *_a, **_k: None)

    with pytest.raises(openclaw_routing.NoCompanyModelError):
        await openclaw_routing.enqueue_openclaw_message(agent=agent, content="hi", await_wake=False)
    row = await openclaw_routing.enqueue_openclaw_message(agent=agent, content="hi", await_wake=False)
    assert loads == 2
    assert row.content == "hi"


@pytest.mark.asyncio
async def test_successful_wake_marks_inbox_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = AgentRecord(id=uuid.uuid4(), name="Guest", creator_id=uuid.uuid4())
    message_id = uuid.uuid4()
    marked: list[tuple[uuid.UUID, uuid.UUID]] = []
    order: list[str] = []
    claimed = GatewayMessageRecord(id=message_id, agent_id=agent.id, content="hello", status="delivered")

    async def fake_wake(_agent: AgentRecord, *, content: str, message_id: uuid.UUID | None) -> bool:
        del content, message_id
        order.append("wake")
        return True

    async def fake_mark(mid: uuid.UUID, agent_id: uuid.UUID) -> GatewayMessageRecord:
        order.append("claim")
        marked.append((mid, agent_id))
        return claimed

    async def fake_release(row: GatewayMessageRecord) -> None:
        pytest.fail(f"successful wake released {row.id}")

    monkeypatch.setattr("app.services.openclaw_inbox.wake_openclaw_inbox", fake_wake)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "mark_delivered", fake_mark)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "release_delivery", fake_release)

    await openclaw_routing._run_inbox_wake(agent, "hello", message_id)
    assert marked == [(message_id, agent.id)]
    assert order == ["claim", "wake"]


class _InboxStore:
    """Mutable queue with SQL-equivalent guards and detached claim snapshots."""

    def __init__(self, agent: AgentRecord) -> None:
        self.row = GatewayMessageRecord(id=uuid.uuid4(), agent_id=agent.id, content="hello")
        self.now = datetime(2026, 10, 14, tzinfo=UTC)
        self.claims: list[GatewayMessageRecord] = []
        self.releases: list[GatewayMessageRecord] = []

    async def mark_delivered(self, message_id: uuid.UUID, agent_id: uuid.UUID) -> GatewayMessageRecord | None:
        if self.row.id != message_id or self.row.agent_id != agent_id or self.row.status != "pending":
            return None
        self.now += timedelta(seconds=1)
        self.row = replace(self.row, status="delivered", delivered_at=self.now)
        self.claims.append(self.row)
        return self.row

    async def claim_pending(self, agent_id: uuid.UUID, *, now: datetime) -> list[GatewayMessageRecord]:
        if self.row.agent_id != agent_id or self.row.status != "pending":
            return []
        self.row = replace(self.row, status="delivered", delivered_at=now)
        return [self.row]

    async def release_delivery(self, claimed: GatewayMessageRecord) -> GatewayMessageRecord | None:
        self.releases.append(claimed)
        if (
            self.row.id != claimed.id
            or self.row.agent_id != claimed.agent_id
            or self.row.status != "delivered"
            or claimed.delivered_at is None
            or self.row.delivered_at != claimed.delivered_at
        ):
            return None
        self.row = replace(self.row, status="pending", delivered_at=None)
        return self.row

    def patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(openclaw_routing.gateway_message_dao, "mark_delivered", self.mark_delivered)
        monkeypatch.setattr(openclaw_routing.gateway_message_dao, "claim_pending", self.claim_pending)
        monkeypatch.setattr(openclaw_routing.gateway_message_dao, "release_delivery", self.release_delivery)


async def test_wake_skips_lost_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = AgentRecord(id=uuid.uuid4(), name="Guest", creator_id=uuid.uuid4())
    store = _InboxStore(agent)
    store.patch(monkeypatch)
    polled = await store.claim_pending(agent.id, now=store.now)

    async def fake_wake(_agent: AgentRecord, *, content: str, message_id: uuid.UUID | None) -> bool:
        pytest.fail("wake dispatched a message already claimed by a poll")

    monkeypatch.setattr("app.services.openclaw_inbox.wake_openclaw_inbox", fake_wake)
    await openclaw_routing._run_inbox_wake(agent, "hello", store.row.id)
    assert polled == [store.row]
    assert store.claims == store.releases == []


@pytest.mark.parametrize("raises", [False, True])
async def test_failed_wake_releases_claim(monkeypatch: pytest.MonkeyPatch, raises: bool) -> None:
    agent = AgentRecord(id=uuid.uuid4(), name="Guest", creator_id=uuid.uuid4())
    store = _InboxStore(agent)
    store.patch(monkeypatch)

    async def fake_wake(_agent: AgentRecord, *, content: str, message_id: uuid.UUID | None) -> bool:
        if raises:
            raise RuntimeError("dispatch failed")
        return False

    monkeypatch.setattr("app.services.openclaw_inbox.wake_openclaw_inbox", fake_wake)
    await openclaw_routing._run_inbox_wake(agent, "hello", store.row.id)
    assert len(store.claims) == 1
    assert store.releases == store.claims
    assert store.releases[0] is store.claims[0]
    assert store.row.status == "pending"
    assert await store.claim_pending(agent.id, now=store.now) == [store.row]


async def test_wake_and_release_errors_are_both_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = AgentRecord(id=uuid.uuid4(), name="Guest", creator_id=uuid.uuid4())
    store = _InboxStore(agent)
    store.patch(monkeypatch)
    warnings: list[tuple[object, ...]] = []
    dispatch_error = RuntimeError("dispatch failed")
    release_error = RuntimeError("release failed")

    async def fake_wake(_agent: AgentRecord, *, content: str, message_id: uuid.UUID | None) -> bool:
        raise dispatch_error

    async def fake_release(claimed: GatewayMessageRecord) -> GatewayMessageRecord | None:
        store.releases.append(claimed)
        raise release_error

    def fake_warning(message: str, *args: object) -> None:
        warnings.append(args)

    monkeypatch.setattr("app.services.openclaw_inbox.wake_openclaw_inbox", fake_wake)
    monkeypatch.setattr(openclaw_routing.gateway_message_dao, "release_delivery", fake_release)
    monkeypatch.setattr(openclaw_routing.logger, "warning", fake_warning)
    await openclaw_routing._run_inbox_wake(agent, "hello", store.row.id)
    assert store.releases == store.claims
    assert len(store.releases) == 1
    assert warnings == [(agent.id, dispatch_error), (agent.id, release_error)]


async def test_completed_during_gateway_wait_skips_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.dao.gateway_message_dao import gateway_message_dao
    from app.services import openclaw_inbox

    agent = AgentRecord(id=uuid.uuid4(), name="Guest", creator_id=uuid.uuid4())
    row = GatewayMessageRecord(id=uuid.uuid4(), agent_id=agent.id, content="hello")
    dispatches: list[str] = []

    def fake_agent_dir(agent_id: uuid.UUID) -> Path:
        return tmp_path

    def fake_tokens(agent_dir: Path) -> tuple[str, str]:
        return "gateway-token", "hooks-token"

    def fake_skill(agent_dir: Path) -> None:
        return None

    def fake_recently_refused(agent_id: uuid.UUID) -> bool:
        return True

    def fake_mark_down(agent_id: uuid.UUID, error: str) -> None:
        return None

    def fake_mark_up(agent_id: uuid.UUID) -> None:
        return None

    async def fake_get(message_id: uuid.UUID, agent_id: uuid.UUID) -> GatewayMessageRecord:
        return row

    async def fake_http(_agent: AgentRecord, agent_dir: Path, body: dict[str, str]) -> str:
        dispatches.append("http")
        return "ECONNREFUSED"

    def fake_wait(_agent: AgentRecord) -> bool:
        nonlocal row
        row = replace(row, status="completed")
        return True

    def fake_exec(_agent: AgentRecord, agent_dir: Path, body: dict[str, str]) -> None:
        dispatches.append("exec")

    monkeypatch.setattr(openclaw_routing.agent_manager, "_agent_dir", fake_agent_dir)
    monkeypatch.setattr(openclaw_inbox, "ensure_openclaw_tokens", fake_tokens)
    monkeypatch.setattr(openclaw_inbox, "write_maraclaw_sync_skill", fake_skill)
    monkeypatch.setattr(openclaw_inbox, "_hooks_recently_refused", fake_recently_refused)
    monkeypatch.setattr(openclaw_inbox, "_mark_hooks_down", fake_mark_down)
    monkeypatch.setattr(openclaw_inbox, "_mark_hooks_up", fake_mark_up)
    monkeypatch.setattr(gateway_message_dao, "get_for_agent", fake_get)
    monkeypatch.setattr(openclaw_inbox, "_http_hooks_wake", fake_http)
    monkeypatch.setattr(openclaw_inbox, "_wait_for_guest_gateway", fake_wait)
    monkeypatch.setattr(openclaw_inbox, "_exec_in_guest_hooks", fake_exec)
    assert await openclaw_inbox.wake_openclaw_inbox(agent, content="hello", message_id=row.id) is True
    assert dispatches == ["http"]


async def test_poll_during_wake_cannot_claim_same_message(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = AgentRecord(id=uuid.uuid4(), name="Guest", creator_id=uuid.uuid4())
    store = _InboxStore(agent)
    store.patch(monkeypatch)
    polled: list[GatewayMessageRecord] = []

    async def fake_wake(_agent: AgentRecord, *, content: str, message_id: uuid.UUID | None) -> bool:
        polled.extend(await store.claim_pending(agent.id, now=store.now))
        store.row = replace(store.row, status="completed")
        return True

    monkeypatch.setattr("app.services.openclaw_inbox.wake_openclaw_inbox", fake_wake)
    await openclaw_routing._run_inbox_wake(agent, "hello", store.row.id)
    assert polled == []
    assert len(store.claims) == 1
    assert await store.mark_delivered(store.row.id, agent.id) is None
    assert await store.release_delivery(store.claims[0]) is None
    assert store.row.status == "completed"


@pytest.mark.parametrize("completed", [False, True])
async def test_failed_wake_stale_release_preserves_newer_state(
    monkeypatch: pytest.MonkeyPatch, completed: bool
) -> None:
    agent = AgentRecord(id=uuid.uuid4(), name="Guest", creator_id=uuid.uuid4())
    store = _InboxStore(agent)
    store.patch(monkeypatch)

    async def fake_wake(_agent: AgentRecord, *, content: str, message_id: uuid.UUID | None) -> bool:
        store.row = replace(store.row, status="pending", delivered_at=None)
        assert await store.claim_pending(agent.id, now=store.now + timedelta(seconds=1)) == [store.row]
        if completed:
            store.row = replace(store.row, status="completed")
        return False

    monkeypatch.setattr("app.services.openclaw_inbox.wake_openclaw_inbox", fake_wake)
    await openclaw_routing._run_inbox_wake(agent, "hello", store.row.id)
    assert len(store.claims) == 1
    assert store.releases == store.claims
    assert store.row.status == ("completed" if completed else "delivered")
    assert store.row.delivered_at != store.claims[0].delivered_at


async def test_inbox_only_completed_is_handled(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.dao.gateway_message_dao import gateway_message_dao
    from app.services.openclaw_inbox import _inbox_already_handled

    agent = AgentRecord(id=uuid.uuid4(), name="Guest", creator_id=uuid.uuid4())
    row: GatewayMessageRecord | None = GatewayMessageRecord(
        id=uuid.uuid4(), agent_id=agent.id, content="hello", status="delivered"
    )
    message_id = row.id

    async def fake_get(mid: uuid.UUID, agent_id: uuid.UUID) -> GatewayMessageRecord | None:
        assert (mid, agent_id) == (message_id, agent.id)
        return row

    monkeypatch.setattr(gateway_message_dao, "get_for_agent", fake_get)
    assert await _inbox_already_handled(agent, message_id) is False
    row = replace(row, status="completed")
    assert await _inbox_already_handled(agent, message_id) is True
    row = None
    assert await _inbox_already_handled(agent, message_id) is False


async def test_generic_wake_never_claims_or_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = AgentRecord(id=uuid.uuid4(), name="Guest", creator_id=uuid.uuid4())
    store = _InboxStore(agent)
    store.patch(monkeypatch)
    wakes: list[uuid.UUID | None] = []

    async def fake_wake(_agent: AgentRecord, *, content: str, message_id: uuid.UUID | None) -> bool:
        wakes.append(message_id)
        if message_id is not None:
            assert len(store.claims) == 1
        return True

    monkeypatch.setattr("app.services.openclaw_inbox.wake_openclaw_inbox", fake_wake)
    await openclaw_routing._run_inbox_wake(agent, "hello")
    assert wakes == [None]
    assert store.claims == store.releases == []
    await openclaw_routing._run_inbox_wake(agent, "hello", store.row.id)
    assert len(store.claims) == 1
    assert store.releases == []
