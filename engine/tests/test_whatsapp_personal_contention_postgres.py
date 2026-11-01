from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import anyio
import pytest
from fastapi import HTTPException
from psycopg.pq import TransactionStatus
from tests.channel_submission_fixtures import channel_db, receipt_db
from tests.test_whatsapp_personal_integration import connected, message_for, target_for
from tests.test_whatsapp_personal_surfaces import use_pool

from app.dao.agent_dao import agent_dao
from app.dao.user_dao import user_dao
from app.dao.whatsapp_personal_dao import claim as claim_personal, get_personal
from app.db import session as db_session
from app.db.errors import LockNotAvailableError
from app.db.session import get_connection, transaction
from app.records.gateway_report_delivery import GatewayReportDeliveryRecord
from app.records.outbound_plan import OutboundChunk
from app.services import gateway_report_relay as relay
from app.services.channels import (
    whatsapp_personal_child as child,
    whatsapp_personal_control as control,
    whatsapp_personal_intake as intake,
    whatsapp_personal_journal as journal,
)
from app.services.channels.whatsapp_personal_journal import PersonalSendPendingError

__all__ = ["channel_db", "receipt_db"]


class LeaseObservedError(Exception):
    pass


@pytest.mark.parametrize("path", ["status", "renew", "dispatch"])
async def test_hot_owner_guards_allow_concurrent_relay_identity(channel_db, monkeypatch, path):
    # Given a real personal connection and a hot path paused while its owner guard is held.
    f = channel_db
    message_id = uuid4()
    async with transaction(f.db.first):
        owner = await connected(f)
        await f.db.first.execute(
            "INSERT INTO gateway_messages(id,agent_id,content,status,result) "
            "VALUES(%(id)s,%(agent)s,'request','completed','synthetic')",
            {"id": message_id, "agent": owner.agent_id},
        )
    claim = GatewayReportDeliveryRecord(uuid4(), message_id, "im", f.db.session_id, tenant_id=f.tenant_id)
    held, release = anyio.Event(), anyio.Event()
    module = {"status": control, "renew": child, "dispatch": journal}[path]
    original = module.personal_owner
    failures = []

    class Pool:
        @asynccontextmanager
        async def connection(self):
            yield f.db.second.raw

    async def guard(*args, **kwargs):
        result = await original(*args, **kwargs)
        held.set()
        await release.wait()
        return result

    async def lease(_self, _value):
        assert get_connection() is None
        raise LeaseObservedError

    monkeypatch.setattr(db_session, "get_pool", lambda: Pool())
    monkeypatch.setattr(module, "personal_owner", guard)
    monkeypatch.setattr(child.PersonalChild, "write", lease)

    async def hot_path():
        match path:
            case "status":
                await control.status(owner.agent_id, owner.owner_id)
            case "renew":
                with pytest.raises(LeaseObservedError):
                    await child.PersonalChild(owner).tick()
            case "dispatch":
                await journal.authorize_dispatch(owner)

    # When relay identity validation runs on another connection before the guard is released.
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(hot_path)
            await held.wait()
            try:
                async with transaction(f.db.first):
                    await relay._lock_identity(claim)
            except LockNotAvailableError as error:
                failures.append(error)
            finally:
                release.set()
    # Then it succeeds concurrently rather than contending with an authorization-only owner read.
    assert failures == []


async def test_personal_inputs_serialize_one_remaining_quota_slot(channel_db, monkeypatch):
    # Given two distinct native inputs and exactly one owner quota slot.
    f = channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
    held, competing, release = anyio.Event(), anyio.Event(), anyio.Event()
    original = intake.personal_owner
    results = []

    async def guard(*args, **kwargs):
        if get_connection() is f.db.second:
            competing.set()
        result = await original(*args, **kwargs)
        if get_connection() is f.db.first:
            held.set()
            await release.wait()
        return result

    monkeypatch.setattr(intake, "personal_owner", guard)

    async def submit(db, native):
        async with transaction(db):
            results.append(await intake.accept_message(owner, message_for(owner, native)))

    # When both real intake transactions compete behind the unchanged exclusive owner mutex.
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(submit, f.db.first, "first")
            await held.wait()
            tasks.start_soon(submit, f.db.second, "second")
            await competing.wait()
            while not await f.db.first.fetchval(
                "SELECT cardinality(pg_blocking_pids(%(pid)s))", {"pid": f.db.second.raw.info.backend_pid}
            ):
                await anyio.lowlevel.checkpoint()
            release.set()
    # Then both are handled without deadlock: one is charged and queued, the other durably rejected for quota.
    assert results == [True, True]
    async with transaction(f.db.first):
        assert await f.db.first.fetchval("SELECT count(*) FROM channel_event_receipts") == 2
        assert await f.db.first.fetchval("SELECT count(*) FROM personal_burst_inputs WHERE outcome = 'accepted'") == 1
        assert (
            await f.db.first.fetchval("SELECT count(*) FROM personal_burst_inputs WHERE outcome = 'quota_rejected'")
            == 1
        )
        assert await f.db.first.fetchval("SELECT count(*) FROM gateway_messages WHERE status = 'pending'") == 1
        assert (
            await f.db.first.fetchval("SELECT quota_messages_used FROM users WHERE id=%(id)s", {"id": owner.owner_id})
            == 1
        )


@pytest.mark.parametrize("refresh", [False, True])
@pytest.mark.parametrize("path", ["status", "renew"])
async def test_expiry_reacquires_exclusive_after_read_commit_and_revalidates(channel_db, monkeypatch, path, refresh):
    # Given an expired pairing discovered under shared owner authorization.
    f = channel_db
    use_pool(f, monkeypatch)
    async with transaction(f.db.first):
        await control.start(f.config.agent_id, f.db.user.id, None)
        pending = await get_personal(f.config.agent_id)
        assert pending is not None
        owner = await claim_personal(pending.config_id)
        assert owner is not None
        await f.db.first.execute(
            "UPDATE whatsapp_personal_connections SET expires_at=%(expired)s WHERE agent_id=%(id)s",
            {"id": owner.agent_id, "expired": datetime.now(UTC) - timedelta(seconds=1)},
        )
    module = {"status": control, "renew": child}[path]
    original = module.personal_owner
    modes = []

    async def revalidate(*args, **kwargs):
        authorization_only = kwargs.get("authorization_only", False)
        modes.append(authorization_only)
        if not authorization_only:
            assert f.db.first.raw.info.transaction_status == TransactionStatus.IDLE
            async with transaction(f.db.second):
                await f.db.second.fetchone(
                    "SELECT id FROM tenants WHERE id=%(id)s FOR UPDATE NOWAIT", {"id": owner.tenant_id}
                )
                await f.db.second.fetchone(
                    "SELECT id FROM users WHERE id=%(id)s FOR UPDATE NOWAIT", {"id": owner.owner_id}
                )
                await f.db.second.fetchone(
                    "SELECT id FROM agents WHERE id=%(id)s FOR UPDATE NOWAIT", {"id": owner.agent_id}
                )
                if refresh:
                    await f.db.second.execute(
                        "UPDATE whatsapp_personal_connections SET expires_at=NULL WHERE agent_id=%(id)s",
                        {"id": owner.agent_id},
                    )
        return await original(*args, **kwargs)

    async def lease(_self, _value):
        assert get_connection() is None
        raise LeaseObservedError

    monkeypatch.setattr(module, "personal_owner", revalidate)
    monkeypatch.setattr(child.PersonalChild, "write", lease)
    # When expiry either remains valid or is refreshed after the read transaction commits.
    with anyio.fail_after(10):
        if path == "status":
            await control.status(owner.agent_id, owner.owner_id)
        elif refresh:
            with pytest.raises(LeaseObservedError):
                await child.PersonalChild(owner).tick()
        else:
            await child.PersonalChild(owner).tick()
    # Then exclusive transition locks never upgrade, and stale expiry cannot revoke a refreshed claim.
    async with transaction(f.db.first):
        current = await get_personal(owner.agent_id)
    assert current is not None
    assert modes[:2] == [True, False]
    assert current.desired is refresh
    assert current.purge_requested is not refresh
    assert (current.generation == owner.generation) is refresh


@pytest.mark.parametrize("revocation", ["inactive", "stopped", "unlink"])
async def test_fresh_revocation_prevents_personal_dispatch(channel_db, revocation):
    # Given a pending immutable personal send and real DAO revocation before dispatch authorization.
    f = channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
        chunk = OutboundChunk(text="synthetic", key=uuid4())
        with pytest.raises(PersonalSendPendingError):
            await journal.enqueue(target_for(owner), chunk)
    async with transaction(f.db.second):
        match revocation:
            case "inactive":
                user = await user_dao.get_with_identity(owner.owner_id, fresh=True)
                assert user is not None
                await user_dao.update(db_obj=user, obj_in={"is_active": False})
            case "stopped":
                agent = await agent_dao.get(owner.agent_id)
                assert agent is not None
                await agent_dao.update(db_obj=agent, obj_in={"status": "stopped"})
            case "unlink":
                await control.disconnect(owner.agent_id, owner.owner_id, owner.generation)
    # When shared dispatch authorization reloads the fresh principal and routing fence.
    if revocation == "inactive":
        with pytest.raises(HTTPException) as denied:
            async with transaction(f.db.first):
                await journal.authorize_dispatch(owner)
        assert denied.value.status_code == 401
    else:
        async with transaction(f.db.first):
            assert await journal.authorize_dispatch(owner) is None
    # Then the journal never reaches dispatch_started and no external sender is invoked.
    async with transaction(f.db.first):
        assert (
            await f.db.first.fetchval(
                "SELECT state FROM whatsapp_personal_sends WHERE command_id=%(id)s", {"id": chunk.key}
            )
            != "dispatch_started"
        )


async def test_stale_child_cannot_expire_current_generation(channel_db, monkeypatch):
    # Given an expired current connection and a child from a different generation.
    f = channel_db
    use_pool(f, monkeypatch)
    async with transaction(f.db.first):
        owner = await connected(f)
        await f.db.first.execute(
            "UPDATE whatsapp_personal_connections SET expires_at=%(expired)s WHERE agent_id=%(id)s",
            {"id": owner.agent_id, "expired": datetime.now(UTC) - timedelta(seconds=1)},
        )
    # When the stale child ticks against the real renewal CAS.
    with anyio.fail_after(10):
        await child.PersonalChild(replace(owner, generation=uuid4())).tick()
    # Then it cannot revoke the current owner of the route.
    async with transaction(f.db.first):
        current = await get_personal(owner.agent_id)
    assert current is not None
    assert current.generation == owner.generation
    assert current.desired
    assert not current.purge_requested
