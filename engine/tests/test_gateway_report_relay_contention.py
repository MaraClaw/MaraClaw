from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from psycopg.errors import DeadlockDetected, LockNotAvailable

from app.db.errors import map_psycopg_error
from app.services import gateway_report_relay as relay, gateway_report_relay_identity as identity
from gateway_report_relay_fakes import RelayStore


@pytest.mark.parametrize("boundary", ["identity_deadlock", "provider_55p03"])
async def test_other_failures_keep_provider_backoff(monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    # Given a sixth attempt failing outside the narrowly typed identity NOWAIT outcome.
    store = RelayStore()
    store.install(monkeypatch)
    row = store.intent("im")
    row.attempts = 5

    class IdentityRead:
        async def fetchone(self, _query: str, _params: object) -> None:
            raise map_psycopg_error(DeadlockDetected("synthetic deadlock"))

    @asynccontextmanager
    async def session():
        yield IdentityRead()

    match boundary:
        case "identity_deadlock":
            monkeypatch.setattr(identity.gateway_message_dao, "session", session)
            monkeypatch.setattr(relay, "_lock_identity", identity.lock_identity)
        case "provider_55p03":
            store.send_error = map_psycopg_error(LockNotAvailable("synthetic provider failure"))
    # When the relay receives a deadlock at acquisition or a lock error from a provider.
    await relay.relay_batch(store.clock)
    # Then it preserves cumulative provider backoff and never checkpoints a failed delivery.
    current = store.rows[row.id]
    assert current.available_at == store.now + timedelta(seconds=32)
    assert current.attempts == 6
    assert current.next_chunk == 0
    assert current.delivered_at is None
