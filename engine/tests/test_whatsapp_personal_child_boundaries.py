import json
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import pytest
from fastapi import HTTPException
from tests.channel_submission_fixtures import channel_db, receipt_db
from tests.test_whatsapp_personal_integration import connected

from app.config import get_settings
from app.dao.user_dao import user_dao
from app.dao.whatsapp_personal_auth_dao import AuthMutation, write_auth
from app.dao.whatsapp_personal_dao import candidates, claim, get_personal, reaped
from app.dao.whatsapp_personal_runtime_dao import RETRY_DELAYS, runtime_for
from app.db.pool import close_pool, init_pool
from app.db.session import transaction
from app.records.whatsapp_personal import PersonalConnection, PersonalRuntime
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


async def test_child_process_receives_the_worker_heap_ceiling_as_a_flag(monkeypatch):
    # Given an operator-chosen heap ceiling.
    monkeypatch.setattr(get_settings(), "WHATSAPP_PERSONAL_WORKER_HEAP_MB", 96)
    # When the real spawn seam starts a process that reports its arguments.
    script = "import json,sys;print(json.dumps(sys.argv[1:]))"
    with anyio.fail_after(10):
        process = await spawn_child((sys.executable, "-c", script))
        async with process:
            assert process.stdout is not None
            raw = b""
            try:
                while True:
                    raw += await process.stdout.receive()
            except anyio.EndOfStream:
                pass
            await process.wait()
    # Then the bridge learns its ceiling from one flag; the environment boundary stays with the test above.
    assert json.loads(raw) == ["--worker-heap-mb=96"]


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


_CRASH = "import sys\nsys.stdin.readline()\nsys.exit(3)\n"
# What the bridge does when its SDK worker runs out of memory: a failure diagnostic, then a failing exit status.
_WORKER_OOM = (
    "import json,sys\n"
    "token=json.loads(sys.stdin.readline())['token']\n"
    "print(json.dumps({'type':'diagnostic','id':'oom','token':token,'stage':'worker_exit','event':'failure'}),flush=True)\n"
    "sys.exit(1)\n"
)
_MALFORMED = (
    "import sys\n"
    "sys.stdin.readline()\n"
    "print('{not json', flush=True)\n"
    "for line in sys.stdin:\n"
    "    if '\"shutdown\"' in line:\n"
    "        break\n"
)


@pytest.mark.parametrize(
    "script", [_CRASH, _MALFORMED, _WORKER_OOM], ids=["nonzero_exit", "malformed_frame", "worker_oom"]
)
async def test_transient_child_failure_keeps_paired_credentials_and_waits_for_retry(channel_db, script):
    # Given a paired, claimed connection with stored credentials.
    f = channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
        await write_auth(owner, AuthMutation("creds", {"main": "secret"}))
    # When the real bridge process dies with a non-zero exit or writes a malformed frame.
    await init_pool(conninfo=f.db.conninfo, min_size=1, max_size=4)
    try:
        with anyio.fail_after(10):
            await run_child(owner, (sys.executable, "-u", "-c", script))
    finally:
        await close_pool()
    # Then credentials and desire survive, the connection waits in reconnecting, and no claim beats retry_at.
    async with transaction(f.db.second):
        row = await get_personal(owner.agent_id)
        runtime = await runtime_for(owner.config_id)
        stored = await f.db.second.fetchval("SELECT count(*) FROM whatsapp_personal_auth")
        waiting = {candidate.config_id for candidate in await candidates()}
        reclaimed = await claim(owner.config_id)
    assert row is not None
    assert row.state == "reconnecting"
    assert row.desired
    assert row.claim_token is None
    assert row.generation == owner.generation
    assert row.account_id == owner.account_id
    assert row.error_code is None
    assert stored == 1
    assert runtime.reconnect_attempts == 1
    assert runtime.retry_at is not None
    assert runtime.retry_at > datetime.now(UTC)
    assert owner.config_id not in waiting
    assert reclaimed is None


async def test_repeated_worker_oom_is_paced_by_the_durable_budget_and_never_revokes_credentials(channel_db):
    # Given a paired, claimed connection with stored credentials whose bridge worker keeps running out of memory.
    f = channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
        await write_auth(owner, AuthMutation("creds", {"main": "secret"}))
    current = owner
    await init_pool(conninfo=f.db.conninfo, min_size=1, max_size=4)
    try:
        for attempt, delay in enumerate(RETRY_DELAYS, start=1):
            before = datetime.now(UTC)
            with anyio.fail_after(10):
                await run_child(current, (sys.executable, "-u", "-c", _WORKER_OOM))
            async with transaction(f.db.second):
                runtime = await runtime_for(owner.config_id)
                waiting = {candidate.config_id for candidate in await candidates()}
                stored = await f.db.second.fetchval("SELECT count(*) FROM whatsapp_personal_auth")
                gated = await claim(owner.config_id)
                # Then each death is charged once with its escalating delay, and nothing respawns before it passes.
                assert runtime.reconnect_attempts == attempt
                assert runtime.retry_at is not None
                assert delay <= (runtime.retry_at - before).total_seconds() < delay + 5
                assert owner.config_id not in waiting
                assert gated is None
                assert stored == 1
                await f.db.second.execute(
                    "UPDATE channel_configs SET extra_config=jsonb_set(extra_config::jsonb,"
                    "'{whatsapp_runtime,retry_at}',to_jsonb(now()-interval '1 second'))::json"
                )
                reclaimed = await claim(owner.config_id)
            assert reclaimed is not None
            current = reclaimed
        # When the budget is spent and the next worker dies the same way.
        with anyio.fail_after(10):
            await run_child(current, (sys.executable, "-u", "-c", _WORKER_OOM))
    finally:
        await close_pool()
    # Then the connection pauses with its credentials kept for the owner's explicit restart, never purged.
    async with transaction(f.db.second):
        row = await get_personal(owner.agent_id)
        stored = await f.db.second.fetchval("SELECT count(*) FROM whatsapp_personal_auth")
        waiting = await candidates()
    assert row is not None
    assert not row.desired
    assert row.state == "paused"
    assert row.error_code == "transport_failed"
    assert row.account_id == owner.account_id
    assert stored == 1
    assert waiting == []


async def test_bridge_transport_close_keeps_credentials_and_claim_until_reap(channel_db):
    # Given a paired, claimed connection with stored credentials.
    f = channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
        await write_auth(owner, AuthMutation("creds", {"main": "secret"}))
    # When the bridge spends its in-process reconnects and reports a transport close.
    async with transaction(f.db.first):
        await handle_frame(owner, Closed(type="closed", id="closed", token="test", reason="transport_failed"))
    # Then the generation, desire and credentials survive and the exact reap leaves a gated reconnect.
    async with transaction(f.db.second):
        row = await get_personal(owner.agent_id)
        stored = await f.db.second.fetchval("SELECT count(*) FROM whatsapp_personal_auth")
        assert await reaped(owner)
        reaped_row = await get_personal(owner.agent_id)
        reclaimed = await claim(owner.config_id)
        runtime = await runtime_for(owner.config_id)
    assert row is not None
    assert row.state == "reconnecting"
    assert row.claim_token == owner.claim_token
    assert row.generation == owner.generation
    assert row.desired
    assert not row.purge_requested
    assert row.error_code is None
    assert stored == 1
    assert reaped_row is not None
    assert reaped_row.state == "reconnecting"
    assert reaped_row.claim_token is None
    assert reclaimed is None
    assert runtime.reconnect_attempts == 1


async def test_lost_owner_authorization_transport_close_is_never_retried(channel_db):
    # Given a paired, claimed connection whose owner is then deactivated.
    f = channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
        await write_auth(owner, AuthMutation("creds", {"main": "secret"}))
    async with transaction(f.db.second):
        user = await user_dao.get_with_identity(owner.owner_id, fresh=True)
        assert user is not None
        await user_dao.update(db_obj=user, obj_in={"is_active": False})
    # When the bridge reports a transport close for that owner.
    async with transaction(f.db.first):
        await handle_frame(owner, Closed(type="closed", id="closed", token="test", reason="transport_failed"))
    # Then the shared code still takes today's terminal path: revoked, paused on reap, no retry budget charged.
    async with transaction(f.db.second):
        row = await get_personal(owner.agent_id)
        runtime = await runtime_for(owner.config_id)
    assert row is not None
    assert not row.desired
    assert row.generation != owner.generation
    assert row.state == "disconnecting"
    assert row.error_code == "transport_failed"
    assert runtime == PersonalRuntime()


@pytest.mark.parametrize(
    ("error", "paired", "consulted", "purged"),
    [
        (RuntimeError("unclassified"), True, False, False),
        # Malformed frames are a bridge defect or version skew, never proof the account is gone: retried.
        (ValueError("malformed frame"), True, True, False),
        (BrokenPipeError("broken pipe"), True, True, False),
        # An unpaired attempt has nothing to keep, so a transient cause still fails it.
        (ValueError("malformed frame"), False, True, True),
        # Lost owner authorization shares transport_failed but is classified by its cause and never retried.
        (HTTPException(401), True, False, True),
    ],
    ids=["unclassified", "malformed_paired", "broken_pipe_paired", "malformed_unpaired", "owner_lost"],
)
async def test_child_errors_are_classified_by_cause(monkeypatch, error, paired, consulted, purged):
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
    interrupted = AsyncMock(return_value=paired)
    monkeypatch.setattr(child_module, "fail_connection", fail_connection)
    monkeypatch.setattr(child_module, "interrupted", interrupted)
    monkeypatch.setattr(child_module, "reaped", AsyncMock(return_value=True))
    monkeypatch.setattr(child_module, "read_auth", AsyncMock(side_effect=error))
    # When run_child handles it.
    if isinstance(error, RuntimeError):
        with pytest.raises(RuntimeError, match="unclassified"):
            await run_child(connection, ("unused",))
    else:
        await run_child(connection, ("unused",))
    # Then transient causes consult the durable retry budget, and only terminal outcomes purge credentials.
    if consulted:
        interrupted.assert_awaited_once_with(connection)
    else:
        interrupted.assert_not_awaited()
    if purged:
        fail_connection.assert_awaited_once_with(connection, "transport_failed")
    else:
        fail_connection.assert_not_awaited()
