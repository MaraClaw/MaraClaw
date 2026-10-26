import httpx
import pytest
from tests.test_gateway_report_relay_postgres import delivery_db, receipt_db, relay_db

from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao
from app.db.session import transaction
from app.services import gateway_report_relay as relay
from test_channel_delivery_transport import transport

__all__ = ["delivery_db", "receipt_db", "relay_db"]


@pytest.mark.parametrize("phase", ["before_preflight", "after_preflight", "during_send"])
async def test_delivery_rechecks_owner_but_finishes_already_running_send(relay_db, monkeypatch, phase):
    db, intent = relay_db
    sent: list[str] = []

    async def disable() -> None:
        async with transaction(db.second):
            await db.second.execute("UPDATE users SET is_active=FALSE WHERE id=%(id)s", {"id": db.user.id})

    async def respond(request: httpx.Request):
        if request.url.path == "/api/auth.test":
            if phase == "after_preflight":
                await disable()
            return httpx.Response(200, json={"ok": True, "team_id": "T1", "bot_id": "B1"})
        sent.append(request.url.path)
        if phase == "during_send":
            await disable()
        return httpx.Response(200, json={"ok": True})

    transport(monkeypatch, respond)
    if phase == "before_preflight":
        await disable()
    assert await relay.relay_batch() == 1
    async with transaction(db.second):
        current = await gateway_report_delivery_dao.get(intent.id)
    assert current is not None
    assert current.next_chunk == (1 if phase == "during_send" else 0)
    assert (current.delivered_at is not None) is (phase == "during_send")
    assert bool(sent) is (phase == "during_send")
    assert current.claim_token is None
    assert current.terminal_at is None
