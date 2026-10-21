from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import anyio
import pytest

from app.records.agent import AgentRecord
from app.records.gateway_report_delivery import TerminalReason
from app.services import gateway_report_relay as relay
from gateway_report_relay_fakes import RelayStore


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RelayStore:
    value = RelayStore()
    value.install(monkeypatch)
    return value


def invalidate(store: RelayStore, reason: TerminalReason) -> None:
    match reason:
        case "source_agent_unavailable":
            store.source.id = uuid4()
        case "source_tenant_changed":
            store.source.tenant_id = uuid4()
        case "destination_agent_unavailable":
            store.destination.id = uuid4()
        case "destination_tenant_changed":
            store.destination.tenant_id = uuid4()
        case "destination_relationship_changed":
            store.message.sender_agent_id = uuid4()
            store.session.agent_id = uuid4()
        case "destination_session_unavailable":
            store.session.id = uuid4()


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("agent", "source_agent_unavailable"),
        ("agent", "source_tenant_changed"),
        ("agent", "destination_agent_unavailable"),
        ("agent", "destination_tenant_changed"),
        ("agent", "destination_relationship_changed"),
        ("im", "source_agent_unavailable"),
        ("im", "source_tenant_changed"),
        ("im", "destination_session_unavailable"),
        ("im", "destination_relationship_changed"),
    ],
)
async def test_persisted_identity_failure_commits_terminal_outcome(
    store: RelayStore, kind: str, reason: TerminalReason
) -> None:
    # Given
    row = store.intent(kind)
    invalidate(store, reason)
    # When
    await relay.relay_batch(store.clock)
    store.now += timedelta(days=1)
    reclaimed = await relay.relay_batch(store.clock)
    # Then
    terminal = store.rows[row.id]
    assert terminal.terminal_reason == reason
    assert terminal.terminal_at == store.now - timedelta(days=1)
    assert terminal.delivered_at is terminal.claim_token is terminal.lease_expires_at is None
    assert store.inbox == store.wakes == store.sends == []
    assert reclaimed == 0


@pytest.mark.parametrize(
    "reason",
    [
        "source_agent_unavailable",
        "source_tenant_changed",
        "destination_agent_unavailable",
        "destination_tenant_changed",
        "destination_relationship_changed",
    ],
)
async def test_identity_failure_during_preparation_is_terminal(store: RelayStore, reason: TerminalReason) -> None:
    # Given
    row = store.intent()
    store.on_prepare = lambda: invalidate(store, reason)
    # When
    await relay.relay_batch(store.clock)
    # Then
    assert store.rows[row.id].terminal_reason == reason
    assert store.inbox == store.wakes == []


async def test_im_session_owner_mismatch_is_terminal(store: RelayStore) -> None:
    # Given
    row = store.intent("im")
    store.session.agent_id = uuid4()
    # When
    await relay.relay_batch(store.clock)
    # Then
    assert store.rows[row.id].terminal_reason == "destination_relationship_changed"
    assert store.sends == []


async def test_advisory_identity_repaired_before_transaction_retries(store: RelayStore) -> None:
    # Given
    row = store.intent()
    original = store.source.tenant_id
    store.source.tenant_id = uuid4()
    store.on_lock = lambda: setattr(store.source, "tenant_id", original)
    # When
    await relay.relay_batch(store.clock)
    # Then
    assert store.rows[row.id].terminal_at is None
    assert store.rows[row.id].available_at == store.now + timedelta(seconds=1)
    assert store.inbox == []
    store.now += timedelta(seconds=1)
    await relay.relay_batch(store.clock)
    assert len(store.inbox) == 1


async def test_report_changed_during_preparation_retries(store: RelayStore) -> None:
    # Given
    row = store.intent()
    store.on_prepare = lambda: setattr(store.message, "result", "new report")
    # When
    await relay.relay_batch(store.clock)
    # Then
    assert store.rows[row.id].terminal_at is None
    assert store.rows[row.id].available_at == store.now + timedelta(seconds=1)
    assert store.inbox == []


@pytest.mark.parametrize("fence", ["rotated", "expired", "terminal"])
async def test_fake_fences_every_write_and_terminal_cannot_revive(store: RelayStore, fence: str) -> None:
    # Given
    store.intent()
    claim = (await store.claim_batch(limit=1, lease_seconds=60, now=store.now))[0]
    assert claim.claim_token is not None
    if fence == "terminal":
        assert await store.finish_terminal(claim.id, claim.claim_token, "source_tenant_changed", now=store.now)
        store.rows[claim.id].claim_token = claim.claim_token
        store.rows[claim.id].lease_expires_at = store.now + timedelta(seconds=60)
    else:
        store.now += timedelta(seconds=60)
        if fence == "rotated":
            await store.claim_batch(limit=1, lease_seconds=60, now=store.now)
    before = replace(store.rows[claim.id])
    # When / Then
    assert not await store.lock_claim(claim, now=store.now)
    assert not await store.acknowledge(claim, now=store.now)
    assert not await store.retry(claim, available_at=store.now, now=store.now)
    assert not await store.finish_terminal(claim.id, claim.claim_token, "destination_tenant_changed", now=store.now)
    assert store.rows[claim.id] == before


async def test_terminal_write_losing_lease_is_not_committed(store: RelayStore) -> None:
    # Given
    row = store.intent("im")
    store.source.tenant_id = uuid4()
    store.on_lock = lambda: setattr(store, "now", store.now + timedelta(seconds=60))
    # When
    await relay.relay_batch(store.clock)
    # Then
    assert store.rows[row.id].terminal_at is None
    assert store.rows[row.id].claim_token is not None
    assert store.sends == []


@pytest.mark.parametrize(("kind", "boundary"), [("agent", "preparation"), ("agent", "lock"), ("im", "lock")])
@pytest.mark.parametrize("error", [ValueError("config"), RuntimeError("config"), OSError("locked"), TimeoutError()])
async def test_transient_failure_recovers_without_attempt_ceiling(
    store: RelayStore, kind: str, boundary: str, error: Exception
) -> None:
    # Given
    row = store.intent(kind)
    row.attempts = 10000

    def fail() -> None:
        raise error

    if boundary == "preparation":
        store.on_prepare = fail
    else:
        store.on_lock = fail
    await relay.relay_batch(store.clock)
    assert store.rows[row.id].terminal_at is None
    assert store.rows[row.id].available_at == store.now + timedelta(seconds=300)
    store.on_prepare = store.on_lock = lambda: None
    store.now += timedelta(seconds=300)
    # When
    await relay.relay_batch(store.clock)
    # Then
    assert store.rows[row.id].delivered_at == store.now
    assert len(store.inbox) + len(store.sends) == 1


async def test_terminal_revalidation_ignores_memoized_agent(store: RelayStore, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    row = store.intent("im")
    cached = replace(store.source)

    async def stale_get(_id: object) -> AgentRecord:
        return cached

    monkeypatch.setattr(relay.agent_dao, "get", stale_get)
    store.source.tenant_id = uuid4()
    # When
    await relay.relay_batch(store.clock)
    # Then
    assert store.rows[row.id].terminal_reason == "source_tenant_changed"
    assert store.sends == []


async def test_agent_cancellation_rolls_back_inbox_and_deferred_wake(store: RelayStore) -> None:
    # Given
    row = store.intent()
    store.block_enqueue = True
    # When
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(relay.relay_batch, store.clock)
        await store.enqueue_entered.wait()
        tasks.cancel_scope.cancel()
    # Then
    assert store.inbox == store.wakes == []
    assert store.rows[row.id].terminal_at is store.rows[row.id].delivered_at is None
    assert store.rows[row.id].claim_token is not None
    store.block_enqueue = False
    store.now += timedelta(seconds=60)
    await relay.relay_batch(store.clock)
    assert len(store.inbox) == 1
    assert store.wakes == ["authoritative"]


async def test_im_identity_change_after_provider_attempt_is_not_terminal(
    store: RelayStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    row = store.intent("im")

    async def send(outbound, chunk) -> None:
        await store.send_chunk(outbound, chunk)
        store.source.tenant_id = uuid4()

    monkeypatch.setattr(relay, "send_chunk", send)
    # When
    await relay.relay_batch(store.clock)
    # Then
    assert store.sends == ["authoritative"]
    assert store.rows[row.id].delivered_at == store.now
    assert store.rows[row.id].terminal_at is None
