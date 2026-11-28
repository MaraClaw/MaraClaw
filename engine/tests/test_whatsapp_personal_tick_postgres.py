from uuid import uuid4

import anyio
import pytest
from fastapi import HTTPException
from tests.channel_submission_fixtures import channel_db, receipt_db
from tests.personal_tick_fixtures import TickStoppedError, between_ticks, queries, record_frames, record_statements
from tests.test_whatsapp_personal_integration import connected, target_for
from tests.test_whatsapp_personal_surfaces import use_pool

from app.dao.agent_dao import agent_dao
from app.dao.user_dao import user_dao
from app.dao.whatsapp_personal_dao import get_personal
from app.db.session import transaction
from app.records.outbound_plan import OutboundChunk
from app.services.channels import (
    whatsapp_personal_child as child,
    whatsapp_personal_control as control,
    whatsapp_personal_journal as journal,
)
from app.services.channels.whatsapp_personal_journal import PersonalSendPendingError

__all__ = ["channel_db", "receipt_db"]


async def test_send_queued_while_idle_is_authorized_and_written_on_the_next_tick(channel_db, monkeypatch):
    # Given an idle connected account whose first tick finds nothing to send.
    f = channel_db
    use_pool(f, monkeypatch)
    async with transaction(f.db.first):
        owner = await connected(f)
    chunk = OutboundChunk(text="synthetic", key=uuid4())
    statements = record_statements(monkeypatch)
    frames = record_frames(monkeypatch, child.PersonalChild)
    idle: list[int] = []

    async def queue():
        idle.append(len(statements))
        async with transaction(f.db.first):
            with pytest.raises(PersonalSendPendingError):
                await journal.enqueue(target_for(owner), chunk)

    between_ticks(monkeypatch, queue)
    # When a send is queued during the idle second.
    with anyio.fail_after(10), pytest.raises(TickStoppedError):
        await child.PersonalChild(owner).tick()
    # Then the idle tick took no lock, and the next tick authorizes the send afresh under the connection lock.
    assert not queries(statements[: idle[0]], "FOR UPDATE")
    assert len(queries(statements[idle[0] :], "FOR UPDATE")) == 2
    assert [frame["type"] for frame in frames] == ["lease", "lease", "send"]
    assert frames[2]["command_id"] == str(chunk.key)
    async with transaction(f.db.first):
        state = await f.db.first.fetchval(
            "SELECT state FROM whatsapp_personal_sends WHERE command_id=%(id)s", {"id": chunk.key}
        )
    assert state == "dispatch_started"


@pytest.mark.parametrize("revocation", ["inactive", "stopped", "unlink"])
async def test_idle_tick_observes_revocation_on_its_next_pass(channel_db, monkeypatch, revocation):
    # Given an idle connected account that has already ticked once.
    f = channel_db
    use_pool(f, monkeypatch)
    async with transaction(f.db.first):
        owner = await connected(f)
    frames = record_frames(monkeypatch, child.PersonalChild)

    async def revoke():
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

    between_ticks(monkeypatch, revoke)
    # When it is revoked, unlinked or stopped during the idle second.
    with anyio.fail_after(10):
        if revocation == "inactive":
            with pytest.raises(HTTPException) as denied:
                await child.PersonalChild(owner).tick()
            assert denied.value.status_code == 401
        else:
            await child.PersonalChild(owner).tick()
    # Then the very next tick ends without renewing or writing a second lease frame.
    assert [frame["type"] for frame in frames] == ["lease"]
    async with transaction(f.db.first):
        current = await get_personal(owner.agent_id)
    assert current is not None
    assert current.desired is (revocation == "inactive")


@pytest.mark.parametrize("answered", [False, True])
async def test_dispatched_send_is_settled_uncertain_only_when_the_bridge_stays_silent(
    channel_db, monkeypatch, answered
):
    # Given a send dispatched by the first tick of a connected account.
    f = channel_db
    use_pool(f, monkeypatch)
    async with transaction(f.db.first):
        owner = await connected(f)
        chunk = OutboundChunk(text="synthetic", key=uuid4())
        with pytest.raises(PersonalSendPendingError):
            await journal.enqueue(target_for(owner), chunk)
    statements = record_statements(monkeypatch)
    record_frames(monkeypatch, child.PersonalChild)
    bounds: list[int] = []

    async def boundary():
        bounds.append(len(statements))

    async def change():
        await boundary()
        async with transaction(f.db.first):
            assert (
                await f.db.first.fetchval(
                    "SELECT state FROM whatsapp_personal_sends WHERE command_id=%(id)s", {"id": chunk.key}
                )
                == "dispatch_started"
            )
            await f.db.first.execute(
                "UPDATE whatsapp_personal_sends SET state=%(state)s,dispatched_at=dispatched_at-interval '26 seconds' "
                "WHERE command_id=%(id)s",
                {"id": chunk.key, "state": "accepted" if answered else "dispatch_started"},
            )
        await boundary()

    between_ticks(monkeypatch, boundary, change, boundary)
    # When a young send is followed by one the bridge answered, or by one silent for over 25 seconds.
    with anyio.fail_after(10), pytest.raises(TickStoppedError):
        await child.PersonalChild(owner).tick()
    # Then each tick reads it by primary key while it is in flight, and only a silent one is written.
    young, settling, idle = (
        statements[bounds[0] : bounds[1]],
        statements[bounds[2] : bounds[3]],
        statements[bounds[3] :],
    )
    assert len(queries(young, "AS silent")) == len(queries(settling, "AS silent")) == 1
    assert not queries(young, "UPDATE whatsapp_personal_sends")
    assert len(queries(settling, "UPDATE whatsapp_personal_sends")) == (0 if answered else 1)
    assert not queries(idle, "AS silent")
    async with transaction(f.db.first):
        row = await f.db.first.fetchone(
            "SELECT state,finished_at IS NOT NULL AS finished FROM whatsapp_personal_sends WHERE command_id=%(id)s",
            {"id": chunk.key},
        )
    assert row == {"state": "accepted", "finished": False} if answered else {"state": "uncertain", "finished": True}


async def test_pending_send_of_another_generation_is_not_work_for_the_tick(channel_db, monkeypatch):
    # Given a pending row from a replaced generation of the same account.
    f = channel_db
    use_pool(f, monkeypatch)
    async with transaction(f.db.first):
        owner = await connected(f)
        await f.db.first.execute(
            "INSERT INTO whatsapp_personal_sends(command_id,config_id,auth_epoch,generation,account_id,self_jid,text,"
            "message_id) VALUES(%(id)s,%(config)s,%(epoch)s,%(generation)s,%(account)s,%(jid)s,'old route','OLD')",
            {
                "id": uuid4(),
                "config": owner.config_id,
                "epoch": owner.auth_epoch,
                "generation": uuid4(),
                "account": owner.account_id,
                "jid": owner.pn_jid,
            },
        )
    statements = record_statements(monkeypatch)
    frames = record_frames(monkeypatch, child.PersonalChild)
    between_ticks(monkeypatch)
    # When the account ticks.
    with anyio.fail_after(10), pytest.raises(TickStoppedError):
        await child.PersonalChild(owner).tick()
    # Then the probe finds no work, so nothing is authorized and no lock is taken.
    assert not queries(statements, "FOR UPDATE")
    assert [frame["type"] for frame in frames] == ["lease"]
