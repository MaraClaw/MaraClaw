from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from app.services import gateway_report_relay as relay
from app.services.channels import outbound_plan
from app.services.channels.outbound_errors import ProviderPayloadRejectedError
from gateway_report_relay_fakes import RelayStore


async def test_later_chunk_failure_resumes_without_replaying_checkpointed_chunks(monkeypatch):
    store = RelayStore()
    store.message.result = "a" * 4000 + "b" * 4000 + "last"
    store.install(monkeypatch)
    row = store.intent("im")
    await relay.relay_batch(store.clock)
    plan = store.rows[row.id].im_plan
    store.send_error = RuntimeError("credentials temporarily unavailable")
    await relay.relay_batch(store.clock)
    assert store.rows[row.id].next_chunk == 1
    assert store.sends == ["a" * 4000]
    store.send_error = None
    available_at = store.rows[row.id].available_at
    assert available_at is not None
    store.now = available_at
    store.message.result = "changed after planning"
    monkeypatch.setattr(outbound_plan, "split_chunks", lambda *_args: pytest.fail("persisted plan was rebuilt"))

    await relay.relay_batch(store.clock)
    await relay.relay_batch(store.clock)

    stored = store.rows[row.id]
    assert stored.im_plan == plan
    assert stored.next_chunk == 3
    assert stored.delivered_at == store.now
    assert stored.claim_token is None
    assert stored.lease_expires_at is None
    assert store.sends == ["a" * 4000, "b" * 4000, "last"]


@pytest.mark.parametrize("change", ["config", "account", "destination", "provider"])
async def test_planned_identity_changes_retry_without_silent_retarget(monkeypatch, change):
    store = RelayStore()
    store.message.result = "a" * 4001
    store.install(monkeypatch)
    row = store.intent("im")
    await relay.relay_batch(store.clock)
    original_config, original_session = replace(store.config), replace(store.session)
    match change:
        case "config":
            store.config.id = uuid4()
        case "account":
            store.config.app_id = "other-account"
        case "destination":
            store.session.external_conv_id = "other-channel"
        case "provider":
            store.session.source_channel = "discord"

    await relay.relay_batch(store.clock)

    stored = store.rows[row.id]
    assert stored.next_chunk == 1
    assert stored.terminal_at is None
    assert store.sends == ["a" * 4000]
    store.config, store.session = original_config, original_session
    assert stored.available_at is not None
    store.now = stored.available_at
    await relay.relay_batch(store.clock)
    assert store.sends == ["a" * 4000, "a"]
    assert store.rows[row.id].delivered_at == store.now


async def test_completed_cursor_acknowledges_without_config_or_resend(monkeypatch):
    store = RelayStore()
    store.install(monkeypatch)
    row = store.intent("im")
    outbound = await _configured(store)
    row.im_plan = outbound_plan.build_plan(outbound.target, "already accepted", row.id)
    row.next_chunk = 1
    store.session.external_conv_id = None

    await relay.relay_batch(store.clock)

    assert store.rows[row.id].delivered_at == store.now
    assert store.sends == []


async def _configured(store):
    async with store.connection():
        return await store.outbound(store.source, store.session)


async def test_expired_chunk_ack_cannot_advance_new_claim(monkeypatch):
    store = RelayStore()
    store.install(monkeypatch)
    row = store.intent("im")
    store.crash_after_send = True
    await relay.relay_batch(store.clock)
    old = replace(store.rows[row.id])
    assert old.next_chunk == 0
    assert old.delivered_at is None
    current = (await store.claim_batch(limit=20, lease_seconds=60, now=store.now))[0]

    advanced = await store.checkpoint_chunk(old, now=store.now)

    assert not advanced
    assert store.rows[row.id].next_chunk == 0
    store.crash_after_send = False
    await relay.deliver_claim(current, store.clock)
    assert store.rows[row.id].delivered_at == store.now
    assert store.sends == ["authoritative", "authoritative"]


@pytest.mark.parametrize("error", [ValueError("config"), RuntimeError("scope"), OSError("network")])
async def test_repairable_chunk_failure_recovers_after_ten_thousand_attempts(monkeypatch, error):
    store = RelayStore()
    store.install(monkeypatch)
    row = store.intent("im")
    row.attempts = 10000
    store.send_error = error

    await relay.relay_batch(store.clock)

    stored = store.rows[row.id]
    assert stored.terminal_at is None
    assert stored.next_chunk == 0
    assert stored.available_at == store.now + timedelta(seconds=300)
    store.send_error = None
    assert stored.available_at is not None
    store.now = stored.available_at
    await relay.relay_batch(store.clock)
    assert store.rows[row.id].delivered_at == store.now


async def test_only_typed_immutable_payload_rejection_is_terminal(monkeypatch):
    store = RelayStore()
    store.install(monkeypatch)
    row = store.intent("im")
    store.send_error = ProviderPayloadRejectedError("slack", "msg_too_long")

    await relay.relay_batch(store.clock)

    stored = store.rows[row.id]
    assert stored.terminal_reason == "payload_rejected"
    assert stored.terminal_at == store.now
    assert stored.next_chunk == 0
    assert stored.delivered_at is None


async def test_one_claim_sends_only_one_chunk(monkeypatch):
    store = RelayStore()
    store.install(monkeypatch)
    store.message.result = "a" * 4001
    row = store.intent("im")

    await relay.relay_batch(store.clock)

    stored = store.rows[row.id]
    assert stored.next_chunk == 1
    assert stored.delivered_at is None
    assert stored.claim_token is None
    assert stored.lease_expires_at is None
    assert stored.available_at == store.now
    assert store.sends == ["a" * 4000]
    assert stored.im_plan is not None
    assert [chunk.text for chunk in stored.im_plan.chunks] == ["a" * 4000, "a"]
