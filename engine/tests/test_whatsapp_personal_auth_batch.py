import sys
from pathlib import Path

import anyio
import pytest
from tests.channel_submission_fixtures import channel_db, receipt_db
from tests.test_whatsapp_personal_surfaces import personal_pool

from app.dao.whatsapp_personal_auth_dao import AuthMutation, write_auth
from app.dao.whatsapp_personal_dao import claim, get_personal
from app.db.errors import CheckViolationError
from app.db.session import transaction
from app.services.channels.whatsapp_personal_child import PersonalChild
from app.services.channels.whatsapp_personal_control import start

__all__ = ["channel_db", "receipt_db"]

_PRELUDE = """
import json,sys
init=json.loads(sys.stdin.readline())
token=init['token']
def emit(value):
 value.update(token=token)
 print(json.dumps(value),flush=True)
def verdict(ident):
 for line in sys.stdin:
  frame=json.loads(line)
  if frame['type']=='result' and frame['id']==ident:
   return frame['ok']
 return None
"""


async def claimed(f):
    async with transaction(f.db.first):
        await start(f.config.agent_id, f.db.user.id, None)
        row = await get_personal(f.config.agent_id)
        assert row is not None
        owned = await claim(row.config_id)
        assert owned is not None
        await write_auth(owned, AuthMutation("session", {"a": "before", "old": "before"}))
        return owned


async def stored(f) -> dict[tuple[str, str], bytes]:
    async with transaction(f.db.second):
        rows = await f.db.second.fetchall("SELECT category,key_id,ciphertext FROM whatsapp_personal_auth")
    return {(row["category"], row["key_id"]): bytes(row["ciphertext"]) for row in rows}


async def test_child_auth_set_commits_every_category_and_null_delete_before_ack(channel_db):
    # Given a claimed session holding two keys and a child sending one multi-category auth_set.
    f = channel_db
    owner = await claimed(f)
    before = await stored(f)
    script = (
        _PRELUDE
        + """
emit({'type':'auth_set','id':'set','batches':[
 {'category':'session','values':{'a':{'type':'Buffer','data':[1]},'old':None}},
 {'category':'future-category','values':{'b':'x'}}]})
assert verdict('set') is True
emit({'type':'closed','id':'closed','reason':'shutdown'})
"""
    )
    # When the parent acknowledges it.
    child = PersonalChild(owner)
    async with personal_pool(f):
        with anyio.fail_after(10):
            await child.run((sys.executable, "-u", "-c", script))
    # Then both categories are committed together and the null entry is deleted.
    after = await stored(f)
    assert child.process is not None
    assert child.process.returncode == 0
    assert set(after) == {("session", "a"), ("future-category", "b")}
    assert after[("session", "a")] != before[("session", "a")]


async def test_failed_second_category_rolls_back_first_and_never_acks_success(channel_db, tmp_path: Path):
    # Given a database that rejects only the second category of one auth_set.
    f = channel_db
    owner = await claimed(f)
    async with transaction(f.db.first):
        await f.db.first.execute(
            "ALTER TABLE whatsapp_personal_auth ADD CONSTRAINT test_only_reject CHECK (category <> 'poison')"
        )
    before = await stored(f)
    marker = tmp_path / "acked"
    script = (
        _PRELUDE
        + f"""
emit({{'type':'auth_set','id':'set','batches':[
 {{'category':'session','values':{{'a':'after','old':None}}}},
 {{'category':'poison','values':{{'b':'x'}}}}]}})
open({str(marker)!r},'w').write(repr(verdict('set')))
"""
    )
    # When the child waits on the ACK.
    child = PersonalChild(owner)
    async with personal_pool(f):
        with anyio.fail_after(10), pytest.raises(ExceptionGroup) as caught:
            await child.run((sys.executable, "-u", "-c", script))
    # Then the first category is unchanged and no successful result was ever delivered.
    assert caught.group_contains(CheckViolationError)
    assert await stored(f) == before
    assert marker.read_text() != "True"
