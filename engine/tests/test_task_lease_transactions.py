from importlib import import_module
from uuid import uuid4

import pytest

from app.dao.task_dao import TaskDAO
from app.db import session
from app.records.task import TaskClaim
from test_db_session import _FakePool, _FakeRawConnection


async def test_renewal_owns_a_committed_transaction_even_inside_caller_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    caller, renewal = _FakeRawConnection(), _FakeRawConnection()
    module = import_module("app.dao.task_dao")
    monkeypatch.setattr(session, "get_pool", lambda: _FakePool(caller))
    monkeypatch.setattr(module, "get_pool", lambda: _FakePool(renewal))

    async def caller_scope() -> None:
        async with session.connection_ctx():
            await TaskDAO().renew(TaskClaim(uuid4(), uuid4()), lease_seconds=120)
            assert renewal.commits == 1
            assert caller.commits == 0
            raise RuntimeError("caller rolls back")

    with pytest.raises(RuntimeError, match="caller rolls back"):
        await caller_scope()
    assert caller.rollbacks == 1
    assert renewal.rollbacks == 0
