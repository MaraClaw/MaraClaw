import os
from collections.abc import Iterator
from typing import Literal, TextIO
from unittest.mock import AsyncMock

import anyio
import pytest
from starlette.types import Message

import user_roster_export_fixtures as fixtures
from app.db.session import get_connection
from app.services import user_roster_export as exporter
from app.services.user_roster_response import RosterDownloadResponse
from app.services.user_roster_tickets import RosterTicketBinding
from user_roster_export_fixtures import TENANT, RosterDatabase, seed_matches

roster_db = fixtures.roster_db


@pytest.fixture
def spools(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[TextIO]]:
    files: list[TextIO] = []
    original = exporter.TemporaryFile

    def create(*, mode: Literal["w+"], encoding: str, newline: str) -> TextIO:
        spool = original(mode=mode, encoding=encoding, newline=newline)
        files.append(spool)
        return spool

    monkeypatch.setattr(exporter, "TemporaryFile", create)
    yield files
    assert files
    assert all(file.closed for file in files)


@pytest.mark.parametrize("failure", ["success", "start", "body", "disconnect", "cancel"])
async def test_response_owns_real_spool_after_snapshot_closes_through_send_execution(
    roster_db: RosterDatabase, spools: list[TextIO], failure: str
):
    await seed_matches(roster_db, 201)
    context = RosterTicketBinding(
        user_id=roster_db.actor.id,
        identity_id=roster_db.actor.id,
        actor_tenant_id=TENANT,
        tenant_id=TENANT,
        search="Match",
        auth_deadline=9999999999,
        auth_generation=0,
    )
    response = RosterDownloadResponse(context, roster_db.actor)
    body_started = anyio.Event()
    messages: list[Message] = []
    scope = anyio.CancelScope()

    async def send(message: Message) -> None:
        assert get_connection() is None
        stats = roster_db.pool.get_stats()
        assert stats["pool_available"] == stats["pool_size"]
        assert len(spools) == 1
        assert not spools[0].closed
        assert os.fstat(spools[0].fileno()).st_nlink == 0
        assert await roster_db.writer.fetchval("SELECT COUNT(*) FROM admin_audit_logs") == 1
        messages.append(message)
        if failure == "start" or (failure == "body" and message["type"] == "http.response.body"):
            raise OSError("interrupted native send")
        if message["type"] == "http.response.body" and failure in ("disconnect", "cancel"):
            body_started.set()
            if failure == "cancel":
                scope.cancel()
            await anyio.sleep_forever()

    async def receive() -> Message:
        await body_started.wait()
        if failure == "disconnect":
            return {"type": "http.disconnect"}
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    with anyio.fail_after(5), scope:
        if failure in ("start", "body"):
            with pytest.raises(OSError, match="interrupted native send"):
                await response({}, receive, send)
        else:
            await response({}, receive, send)
    if failure == "success":
        payload = b"".join(message.get("body", b"") for message in messages)
        assert len(payload.splitlines()) == 202
        headers = dict(messages[0]["headers"])
        assert int(headers[b"content-length"]) == len(payload)


async def test_disconnect_during_generation_releases_spool_without_audit(
    roster_db: RosterDatabase, spools: list[TextIO], monkeypatch: pytest.MonkeyPatch
):
    started = anyio.Event()
    audit = AsyncMock()
    monkeypatch.setattr(exporter, "write_admin_audit", audit)

    async def page(*args: object, **kwargs: object) -> None:
        started.set()
        await anyio.sleep_forever()

    monkeypatch.setattr(exporter.user_dao, "list_for_tenant_page", page)
    context = RosterTicketBinding(
        user_id=roster_db.actor.id,
        identity_id=roster_db.actor.id,
        actor_tenant_id=TENANT,
        tenant_id=TENANT,
        search="",
        auth_deadline=9999999999,
        auth_generation=0,
    )

    async def receive() -> Message:
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        pytest.fail("generation was interrupted before response headers")

    with anyio.fail_after(5):
        await RosterDownloadResponse(context, roster_db.actor)({}, receive, send)
    audit.assert_not_awaited()
    async with roster_db.pool.connection() as raw:
        assert await fixtures.DbConnection(raw).fetchval("SHOW transaction_read_only") == "off"
