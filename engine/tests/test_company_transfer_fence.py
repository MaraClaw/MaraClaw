import fcntl
from pathlib import Path

import pytest

from app.db import session
from app.services.agent_setup_lock import setup_workspace_lock
from test_db_session import _FakePool, _FakeRawConnection


@pytest.mark.parametrize("rollback", [False, True])
async def test_workspace_fence_is_held_until_owning_transaction_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rollback: bool
) -> None:
    # Given a real flock with a fake owning database transaction.
    raw = _FakeRawConnection()
    monkeypatch.setattr(session, "get_pool", lambda: _FakePool(raw))
    token = session._conn_ctx.set(None)
    directory = tmp_path / "agent"
    lock_path = tmp_path / "_setup_locks/agent.lock"
    # When the service's lock context exits before its HTTP transaction commits/rolls back.
    try:
        try:
            async with session.connection_ctx():
                async with setup_workspace_lock(directory, until_transaction_end=True):
                    pass
                with lock_path.open("a+b") as contender, pytest.raises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if rollback:
                    raise ValueError("rollback")
        except ValueError:
            if not rollback:
                raise
        # Then either transaction outcome releases the OS lock for the next lifecycle operation.
        with lock_path.open("a+b") as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(contender, fcntl.LOCK_UN)
        assert (raw.commits, raw.rollbacks) == ((0, 1) if rollback else (1, 0))
    finally:
        session._conn_ctx.reset(token)
