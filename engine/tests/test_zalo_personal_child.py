import json
import sys
from uuid import uuid4

import anyio
import pytest
from tests.zalo_personal_fixtures import bind, channel_db, claimed, receipt_db, route_for

from app.dao.zalo_personal_dao import claim, get_personal
from app.db.pool import close_pool, init_pool
from app.db.session import transaction
from app.records.outbound_plan import OutboundChunk
from app.services.channels import zalo_personal_child as child_module
from app.services.channels.whatsapp_personal_journal import PersonalSendPendingError
from app.services.channels.zalo_personal_child import ZaloChild, spawn_child
from app.services.channels.zalo_personal_journal import enqueue
from app.services.channels.zalo_personal_protocol import Ready, SendResult

__all__ = ["channel_db", "receipt_db"]

_BRIDGE = """
import json,sys,psycopg
init=json.loads(sys.stdin.readline())
assert init['type']=='init' and init['lease_ms']==30000
token=init['token']
def emit(kind,**fields):
    print(json.dumps(dict(id=kind,token=token,type=kind,**fields)),flush=True)
def ready():
    emit('identity',account_id='123456789')
    emit('ready')
if init['credentials'] is None:
    emit('creds_set',creds='opaque-session')
else:
    assert init['credentials']=='opaque-session'
    ready()
for line in sys.stdin:
    frame=json.loads(line)
    assert frame['token']==token
    if frame['type']=='result':
        assert frame['request_id']=='creds_set' and frame['ok']
        with psycopg.connect(sys.argv[1]) as db:
            assert db.execute('SELECT count(*) FROM zalo_personal_auth').fetchone()[0]==1
        ready()
    elif frame['type']=='send':
        assert frame['thread_id']=='987654321' and frame['text']=='reply'
        emit('send_result',command_id=frame['command_id'],message_id='sdk-message',outcome='accepted')
    elif frame['type']=='shutdown':
        emit('closed',reason='shutdown')
        break
    else:
        assert frame['type']=='lease' and frame['lease_ms']==30000
"""


async def test_real_subprocess_commits_before_ack_dispatches_and_restores(channel_db, monkeypatch):
    f = channel_db
    async with transaction(f.db.first):
        owned = await claimed(f)
    ready = anyio.Event()
    sent = anyio.Event()
    original = child_module.handle_frame

    async def observed(connection, frame):
        await original(connection, frame)
        if isinstance(frame, Ready):
            ready.set()
        if isinstance(frame, SendResult):
            sent.set()

    monkeypatch.setattr(child_module, "handle_frame", observed)
    await init_pool(conninfo=f.db.conninfo, min_size=1, max_size=4)
    try:
        child = ZaloChild(owned)
        with anyio.fail_after(15):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(child.run, (sys.executable, "-u", "-c", _BRIDGE, f.db.conninfo))
                await ready.wait()
                async with transaction(f.db.first):
                    current = await get_personal(owned.agent_id)
                    assert current is not None
                    assert current.state == "connected"
                    await bind(f, current)
                    destination = route_for(current).external_id()
                chunk = OutboundChunk(text="reply", key=uuid4())
                with pytest.raises(PersonalSendPendingError):
                    await enqueue(destination, chunk)
                await sent.wait()
                await enqueue(destination, chunk)
                tasks.cancel_scope.cancel()
        assert child.process is not None
        assert child.process.returncode == 0
        async with transaction(f.db.first):
            current = await get_personal(owned.agent_id)
            assert current is not None
            assert current.state == "reconnecting"
            assert current.claim_token is None
            restored = await claim(current.config_id)
            assert restored is not None
        ready = anyio.Event()
        replacement = ZaloChild(restored)
        with anyio.fail_after(15):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(replacement.run, (sys.executable, "-u", "-c", _BRIDGE, f.db.conninfo))
                await ready.wait()
                tasks.cancel_scope.cancel()
        assert replacement.process is not None
        assert replacement.process.returncode == 0
    finally:
        await close_pool()


async def test_spawn_isolates_parent_secrets(monkeypatch):
    secrets = ("DATABASE_URL", "SECRET_KEY", "NODE_OPTIONS", "HOME", "HTTPS_PROXY")
    for name in secrets:
        monkeypatch.setenv(name, "private")
    process = await spawn_child((sys.executable, "-c", "import os,json;print(json.dumps(dict(os.environ)))"))
    async with process:
        assert process.stdout is not None
        raw = b""
        try:
            while True:
                raw += await process.stdout.receive()
        except anyio.EndOfStream:
            pass
        assert await process.wait() == 0
    assert not set(secrets) & set(json.loads(raw))
