import json
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import pytest
from tests.channel_submission_fixtures import channel_db, receipt_db
from tests.test_whatsapp_personal_integration import connected

from app.dao.whatsapp_personal_dao import claim, get_personal, reaped
from app.db.pool import close_pool, init_pool
from app.db.session import transaction
from app.records.whatsapp_personal import PersonalConnection
from app.services.channels import whatsapp_personal_child as child_module
from app.services.channels.whatsapp_personal_child import run_child, spawn_child
from app.services.channels.whatsapp_personal_control import start
from app.services.channels.whatsapp_personal_events import handle_frame
from app.services.channels.whatsapp_personal_protocol import Closed

__all__ = ["channel_db", "receipt_db"]

_LEAKS = (
    "DATABASE_URL",
    "JWT_SECRET_KEY",
    "PLATFORM_ADMIN_PASSWORD",
    "OPENAI_API_KEY",
    "NODE_OPTIONS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "MARACLAW_CANARY",
    "HOME",
)


async def test_child_process_receives_only_executable_lookup_environment(monkeypatch):
    # Given engine secrets and runtime-altering variables in the parent process environment.
    for name in _LEAKS:
        monkeypatch.setenv(name, "leak")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    # When the real spawn seam starts a process that reports its actual environment.
    with anyio.fail_after(10):
        process = await spawn_child((sys.executable, "-c", "import json,os;print(json.dumps(dict(os.environ)))"))
        async with process:
            assert process.stdout is not None
            raw = b""
            try:
                while True:
                    raw += await process.stdout.receive()
            except anyio.EndOfStream:
                pass
            await process.wait()
    child_env = json.loads(raw)
    # Then only the executable search path crosses the boundary.
    assert child_env["PATH"] == "/usr/bin:/bin"
    assert not set(_LEAKS) & set(child_env)


async def test_non_ascii_child_token_fails_closed_without_crashing_supervisor(channel_db):
    # Given a claimed connection whose child answers with a non-ASCII token.
    f = channel_db
    async with transaction(f.db.first):
        await start(f.config.agent_id, f.db.user.id, None)
        owner = await claim(f.config.id)
        assert owner is not None
    script = (
        "import json,sys\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'ready','id':'r','token':'t\\u00f6ken'}),flush=True)\n"
    )
    # When run_child consumes the malformed frame.
    await init_pool(conninfo=f.db.conninfo, min_size=1, max_size=4)
    try:
        with anyio.fail_after(10):
            await run_child(owner, (sys.executable, "-u", "-c", script))
    finally:
        await close_pool()
    # Then only that connection fails closed and the exception never reaches the manager.
    async with transaction(f.db.second):
        row = await get_personal(owner.agent_id)
        assert row is not None
        assert row.state == "failed"
        assert row.error_code == "transport_failed"
        assert row.claim_token is None
        assert not row.desired


async def test_stale_child_closed_does_not_cancel_queued_relink(channel_db):
    # Given a claimed child whose connection was relinked while it was still alive.
    f = channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
        await start(owner.agent_id, f.db.user.id, owner.generation, relink=True)
        relinked = await get_personal(owner.agent_id)
        assert relinked is not None
        assert relinked.generation != owner.generation
        assert relinked.desired
    # When the old child reports that it was logged out.
    async with transaction(f.db.first):
        await handle_frame(owner, Closed(type="closed", id="closed", token="test", reason="logged_out"))
    # Then the queued relink keeps its generation and desire, and the exact reap still restarts it.
    async with transaction(f.db.second):
        row = await get_personal(owner.agent_id)
        assert row is not None
        assert row.desired
        assert row.generation == relinked.generation
        assert row.error_code is None
        assert await reaped(owner)
        row = await get_personal(owner.agent_id)
        assert row is not None
        assert row.state == "pending"
        assert row.claim_token is None


async def test_current_child_closed_logged_out_revokes_and_purges(channel_db):
    # Given the current claimed child for the current generation.
    f = channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
    # When that child reports a server-side logout.
    async with transaction(f.db.first):
        await handle_frame(owner, Closed(type="closed", id="closed", token="test", reason="logged_out"))
    # Then the connection is revoked with the logged-out reason pending the physical reap.
    async with transaction(f.db.second):
        row = await get_personal(owner.agent_id)
        assert row is not None
        assert not row.desired
        assert row.generation != owner.generation
        assert row.purge_requested
        assert row.error_code == "logged_out"
        assert row.state == "disconnecting"


@pytest.mark.parametrize(
    ("error", "classified"), [(RuntimeError("unclassified"), False), (ValueError("classified"), True)]
)
async def test_only_classified_child_errors_fail_the_connection(monkeypatch, error, classified):
    # Given a claimed connection whose initial credential read raises.
    connection = PersonalConnection(
        config_id=uuid4(),
        agent_id=uuid4(),
        tenant_id=uuid4(),
        owner_id=uuid4(),
        auth_epoch=uuid4(),
        generation=uuid4(),
        attempt_id=uuid4(),
        revision=1,
        state="pending",
        desired=True,
        created_at=datetime.now(UTC),
    )
    fail_connection = AsyncMock()
    monkeypatch.setattr(child_module, "fail_connection", fail_connection)
    monkeypatch.setattr(child_module, "reaped", AsyncMock(return_value=True))
    monkeypatch.setattr(child_module, "read_auth", AsyncMock(side_effect=error))
    # When run_child handles it.
    if classified:
        await run_child(connection, ("unused",))
    else:
        with pytest.raises(RuntimeError, match="unclassified"):
            await run_child(connection, ("unused",))
    # Then credentials are purged (fail_connection) only for classified errors.
    if classified:
        fail_connection.assert_awaited_once_with(connection, "transport_failed")
    else:
        fail_connection.assert_not_awaited()
