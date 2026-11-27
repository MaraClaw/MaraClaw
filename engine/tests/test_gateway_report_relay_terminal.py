from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import anyio
import pytest

from app.config import get_settings
from app.records.agent import AgentRecord
from app.records.gateway_report_delivery import TerminalReason
from app.records.outbound_plan import OutboundProvider
from app.services import gateway_report_relay as relay
from app.services.channels.outbound_errors import ProviderDeliveryError, ProviderPermanentError
from app.services.channels.whatsapp_window import WhatsAppReplyWindowError
from gateway_report_relay_fakes import RelayStore


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RelayStore:
    value = RelayStore()
    value.install(monkeypatch)
    return value


@pytest.mark.parametrize("reason", ["whatsapp_window_unavailable", "whatsapp_window_expired"])
async def test_reply_window_terminal_preserves_checkpoint_and_claim_fence(store, reason):
    row = store.intent("im")
    store.message.result = "a" * 4001
    await relay.relay_batch(store.clock)
    store.send_error = WhatsAppReplyWindowError(reason)
    await relay.relay_batch(store.clock)
    terminal = store.rows[row.id]
    assert terminal.terminal_reason == reason
    assert terminal.next_chunk == 1
    assert terminal.delivered_at is None
    assert terminal.claim_token is terminal.lease_expires_at is None
    store.send_error = None
    store.now += timedelta(days=1)
    assert await relay.relay_batch(store.clock) == 0
    assert store.sends == ["a" * 4000]


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
async def test_transient_failure_recovers_on_the_last_attempt_below_the_ceiling(
    store: RelayStore, kind: str, boundary: str, error: Exception
) -> None:
    # Given
    row = store.intent(kind)
    row.attempts = get_settings().GATEWAY_REPORT_MAX_ATTEMPTS - 2

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


@pytest.mark.parametrize(
    ("provider", "code"),
    [(OutboundProvider.SLACK, "channel_not_found"), (OutboundProvider.SLACK, "is_archived")],
)
async def test_permanent_provider_error_fails_on_first_sight_with_its_reason(
    store: RelayStore, provider: OutboundProvider, code: str
) -> None:
    # Given a provider that says the destination is gone
    row = store.intent("im")
    store.send_error = ProviderPermanentError(provider, code)
    # When
    first = await relay.relay_batch(store.clock)
    again = await relay.relay_batch(store.clock)
    # Then the delivery is failed once, keeps the provider's reason and is never claimed again
    stored = store.rows[row.id]
    assert (first, again) == (1, 0)
    assert stored.attempts == 1
    assert stored.terminal_reason == "provider_permanent_error"
    assert stored.failure_detail == f"{provider}: {code}"
    assert stored.terminal_at == store.now
    assert stored.delivered_at is None
    assert store.sends == []


async def test_transient_provider_error_keeps_retrying_and_honors_retry_after(store: RelayStore) -> None:
    # Given a provider error that is not permanent and asks for a delay longer than our own
    row = store.intent("im")
    store.send_error = ProviderDeliveryError(OutboundProvider.SLACK, "ratelimited", store.now + timedelta(seconds=900))
    # When
    await relay.relay_batch(store.clock)
    # Then
    stored = store.rows[row.id]
    assert stored.terminal_at is None
    assert stored.failure_detail is None
    assert stored.available_at == store.now + timedelta(seconds=900)


@pytest.mark.parametrize(("jitter", "seconds"), [(0.0, 150), (1.0, 300)])
async def test_retry_delay_is_jittered_and_never_passes_the_ceiling(
    store: RelayStore, jitter: float, seconds: int
) -> None:
    # Given a delivery deep into its backoff and an injected random source
    row = store.intent("im")
    row.attempts = get_settings().GATEWAY_REPORT_MAX_ATTEMPTS - 2
    store.send_error = OSError("provider")
    store.jitter = jitter
    # When
    await relay.relay_batch(store.clock)
    # Then
    assert store.rows[row.id].available_at == store.now + timedelta(seconds=seconds)


async def test_attempt_limit_marks_the_failing_delivery_failed_without_storing_the_raw_error(
    store: RelayStore,
) -> None:
    # Given a delivery whose next claim is its last allowed attempt
    row = store.intent("im")
    row.attempts = get_settings().GATEWAY_REPORT_MAX_ATTEMPTS - 1
    store.send_error = OSError("secret-token in provider body")
    # When
    await relay.relay_batch(store.clock)
    # Then
    stored = store.rows[row.id]
    assert stored.attempts == get_settings().GATEWAY_REPORT_MAX_ATTEMPTS
    assert stored.terminal_reason == "attempts_exhausted"
    assert stored.failure_detail == "OSError"
    assert stored.terminal_at == store.now


async def test_age_limit_marks_the_failing_delivery_failed(store: RelayStore) -> None:
    # Given a delivery that has been failing for the whole age window
    row = store.intent("im")
    row.created_at = store.now - timedelta(hours=get_settings().GATEWAY_REPORT_MAX_AGE_HOURS)
    store.send_error = ProviderDeliveryError(OutboundProvider.SLACK, "ratelimited")
    # When
    await relay.relay_batch(store.clock)
    # Then
    stored = store.rows[row.id]
    assert stored.terminal_reason == "age_exhausted"
    assert stored.failure_detail == "slack: ratelimited"


async def test_a_delivery_that_succeeds_on_its_last_attempt_is_delivered(store: RelayStore) -> None:
    # Given
    row = store.intent("im")
    row.attempts = get_settings().GATEWAY_REPORT_MAX_ATTEMPTS - 1
    row.created_at = store.now - timedelta(hours=get_settings().GATEWAY_REPORT_MAX_AGE_HOURS + 1)
    # When
    await relay.relay_batch(store.clock)
    # Then limits apply to failures only
    assert store.rows[row.id].delivered_at == store.now
    assert store.rows[row.id].terminal_at is None


async def test_each_claim_batch_carries_the_per_tenant_cap(store: RelayStore) -> None:
    # Given
    store.intent("im")
    # When
    await relay.relay_batch(store.clock)
    # Then
    assert store.claim_calls == [get_settings().GATEWAY_REPORT_TENANT_CLAIM_LIMIT]
