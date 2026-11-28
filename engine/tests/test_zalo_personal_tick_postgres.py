from uuid import uuid4

import anyio
import pytest
from fastapi import HTTPException
from tests.personal_tick_fixtures import TickStoppedError, between_ticks, queries, record_frames, record_statements
from tests.test_whatsapp_personal_surfaces import use_pool
from tests.zalo_personal_fixtures import bind, channel_db, connected, receipt_db, route_for

from app.dao.agent_dao import agent_dao
from app.dao.user_dao import user_dao
from app.dao.zalo_personal_dao import fenced, renew
from app.db.session import transaction
from app.records.outbound_plan import OutboundChunk
from app.services.channels import zalo_personal_child as child_module
from app.services.channels.whatsapp_personal_journal import PersonalSendPendingError
from app.services.channels.zalo_personal_child import ZaloChild
from app.services.channels.zalo_personal_control import disconnect
from app.services.channels.zalo_personal_journal import enqueue

__all__ = ["channel_db", "receipt_db"]

_MEDIA_ROW = (
    "INSERT INTO zalo_personal_media_sends(command_id,config_id,auth_epoch,generation,account_id,peer_id,file) "
    "VALUES(%(id)s,%(config)s,%(epoch)s,%(generation)s,%(account)s,'987654321','{}'::jsonb)"
)


async def tick_with_tasks(running: ZaloChild) -> None:
    async with anyio.create_task_group() as tasks:
        running.tasks = tasks
        with pytest.raises(TickStoppedError):
            await running.tick()


@pytest.mark.parametrize("kind", ["text", "media"])
async def test_work_queued_while_idle_is_picked_up_on_the_next_tick(channel_db, monkeypatch, kind):
    # Given an idle connected account whose first tick finds nothing to send.
    f = channel_db
    use_pool(f, monkeypatch)
    async with transaction(f.db.first):
        owned = await connected(f)
        await bind(f, owned)
    chunk = OutboundChunk(text="reply", key=uuid4())
    statements = record_statements(monkeypatch)
    frames = record_frames(monkeypatch, ZaloChild)
    media_jobs: list[ZaloChild] = []

    async def media_job(running: ZaloChild) -> None:
        media_jobs.append(running)
        running.media_busy = False

    monkeypatch.setattr(child_module, "send_pending_media", media_job)
    idle: list[int] = []

    async def queue():
        idle.append(len(statements))
        async with transaction(f.db.first):
            if kind == "media":
                await f.db.first.execute(
                    _MEDIA_ROW,
                    {
                        "id": chunk.key,
                        "config": owned.config_id,
                        "epoch": owned.auth_epoch,
                        "generation": owned.generation,
                        "account": owned.account_id,
                    },
                )
            else:
                with pytest.raises(PersonalSendPendingError):
                    await enqueue(route_for(owned).external_id(), chunk)

    between_ticks(monkeypatch, queue)
    # When work is queued during the idle second.
    with anyio.fail_after(10):
        await tick_with_tasks(ZaloChild(owned))
    # Then the idle tick wakes nothing and took no lock, and the next one hands the work over.
    assert not queries(statements[: idle[0]], "FOR UPDATE")
    assert [frame["type"] for frame in frames] == ["lease", "lease", *(["send"] if kind == "text" else [])]
    assert len(media_jobs) == (kind == "media")
    if kind == "text":
        assert frames[2]["command_id"] == str(chunk.key)
        async with transaction(f.db.first):
            assert (
                await f.db.first.fetchval(
                    "SELECT state FROM zalo_personal_sends WHERE command_id=%(id)s", {"id": chunk.key}
                )
                == "dispatch_started"
            )


async def test_pending_rows_of_another_generation_are_not_work_for_the_tick(channel_db, monkeypatch):
    # Given pending text and media rows from a replaced generation of the same account.
    f = channel_db
    use_pool(f, monkeypatch)
    async with transaction(f.db.first):
        owned = await connected(f)
        old = {"config": owned.config_id, "epoch": owned.auth_epoch, "generation": uuid4(), "account": owned.account_id}
        await f.db.first.execute(_MEDIA_ROW, {**old, "id": uuid4()})
        await f.db.first.execute(
            "INSERT INTO zalo_personal_sends(command_id,config_id,auth_epoch,generation,account_id,peer_id,text) "
            "VALUES(%(id)s,%(config)s,%(epoch)s,%(generation)s,%(account)s,'987654321','old route')",
            {**old, "id": uuid4()},
        )
    statements = record_statements(monkeypatch)
    frames = record_frames(monkeypatch, ZaloChild)
    media_jobs: list[ZaloChild] = []

    async def media_job(running: ZaloChild) -> None:
        media_jobs.append(running)

    monkeypatch.setattr(child_module, "send_pending_media", media_job)
    between_ticks(monkeypatch)
    # When the account ticks.
    with anyio.fail_after(10):
        await tick_with_tasks(ZaloChild(owned))
    # Then the probes find no work, so nothing is authorized or started and no lock is taken.
    assert not queries(statements, "FOR UPDATE")
    assert media_jobs == []
    assert [frame["type"] for frame in frames] == ["lease"]


@pytest.mark.parametrize("revocation", ["inactive", "stopped", "unlink"])
async def test_idle_tick_observes_revocation_on_its_next_pass(channel_db, monkeypatch, revocation):
    # Given an idle connected account that has already ticked once.
    f = channel_db
    use_pool(f, monkeypatch)
    async with transaction(f.db.first):
        owned = await connected(f)
    frames = record_frames(monkeypatch, ZaloChild)

    async def revoke():
        async with transaction(f.db.second):
            match revocation:
                case "inactive":
                    user = await user_dao.get_with_identity(owned.owner_id, fresh=True)
                    assert user is not None
                    await user_dao.update(db_obj=user, obj_in={"is_active": False})
                case "stopped":
                    agent = await agent_dao.get(owned.agent_id)
                    assert agent is not None
                    await agent_dao.update(db_obj=agent, obj_in={"status": "stopped"})
                case "unlink":
                    await disconnect(owned.agent_id, f.db.user.id, owned.generation)

    between_ticks(monkeypatch, revoke)
    # When it is revoked, unlinked or stopped during the idle second.
    with anyio.fail_after(10), pytest.raises(HTTPException):
        await ZaloChild(owned).tick()
    # Then the very next tick fails its fence without renewing or writing a second lease frame.
    assert [frame["type"] for frame in frames] == ["lease"]


@pytest.mark.parametrize("answered", [False, True])
async def test_dispatched_send_is_settled_uncertain_only_when_the_bridge_stays_silent(
    channel_db, monkeypatch, answered
):
    # Given a send dispatched by the first tick of a connected account.
    f = channel_db
    use_pool(f, monkeypatch)
    async with transaction(f.db.first):
        owned = await connected(f)
        await bind(f, owned)
        chunk = OutboundChunk(text="reply", key=uuid4())
        with pytest.raises(PersonalSendPendingError):
            await enqueue(route_for(owned).external_id(), chunk)
    statements = record_statements(monkeypatch)
    record_frames(monkeypatch, ZaloChild)
    bounds: list[int] = []

    async def boundary():
        bounds.append(len(statements))

    async def change():
        await boundary()
        async with transaction(f.db.first):
            await f.db.first.execute(
                "UPDATE zalo_personal_sends SET state=%(state)s,dispatched_at=dispatched_at-interval '26 seconds' "
                "WHERE command_id=%(id)s AND state='dispatch_started'",
                {"id": chunk.key, "state": "accepted" if answered else "dispatch_started"},
            )
        await boundary()

    between_ticks(monkeypatch, boundary, change, boundary)
    # When a young send is followed by one the bridge answered, or by one silent for over 25 seconds.
    with anyio.fail_after(10), pytest.raises(TickStoppedError):
        await ZaloChild(owned).tick()
    # Then each tick reads it by primary key while it is in flight, and only a silent one is written.
    young, settling, idle = (
        statements[bounds[0] : bounds[1]],
        statements[bounds[2] : bounds[3]],
        statements[bounds[3] :],
    )
    assert len(queries(young, "AS silent")) == len(queries(settling, "AS silent")) == 1
    assert not queries(young, "UPDATE zalo_personal_sends")
    assert len(queries(settling, "UPDATE zalo_personal_sends")) == (0 if answered else 1)
    assert not queries(idle, "AS silent")
    async with transaction(f.db.first):
        row = await f.db.first.fetchone(
            "SELECT state,finished_at IS NOT NULL AS finished FROM zalo_personal_sends WHERE command_id=%(id)s",
            {"id": chunk.key},
        )
    assert row == {"state": "accepted", "finished": False} if answered else {"state": "uncertain", "finished": True}


@pytest.mark.parametrize("change", ["lease", "claim"])
async def test_renew_with_the_fenced_row_still_fails_closed(channel_db, change):
    # Given a lease fenced in this transaction, then lost or taken over before its renewal.
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        current = await fenced(owned, lock=False)
        if change == "lease":
            await f.db.first.execute("UPDATE zalo_personal_connections SET lease_until=now()-interval '1 second'")
        else:
            await f.db.first.execute("UPDATE zalo_personal_connections SET claim_token=%(claim)s", {"claim": uuid4()})
        lease = await f.db.first.fetchval("SELECT lease_until FROM zalo_personal_connections")
        # When it is renewed without a second fence.
        renewed = await renew(owned, current)
        kept = await f.db.first.fetchval("SELECT lease_until FROM zalo_personal_connections")
    # Then only its own conditional UPDATE refuses, and the lease is not extended.
    assert not renewed
    assert kept == lease
