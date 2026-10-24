import fcntl
from pathlib import Path
from uuid import uuid4

import anyio
import pytest
from fastapi import HTTPException

from app.db import session
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.services import company_transfer_fence as fence
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


async def test_busy_workspace_rejects_final_approval_with_retryable_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an owned workspace still locked when the total fence wait budget expires.
    user = UserRecord(id=uuid4())
    agent = AgentRecord(id=uuid4(), name="Busy", creator_id=user.id)
    locks = tmp_path / "_setup_locks"
    locks.mkdir()
    clock = iter((0.0, 5.0))

    async def owned(_id: object, *, tenant_id: object) -> list[AgentRecord]:
        return [agent]

    async def allowed(_id: object) -> None:
        return None

    monkeypatch.setattr(fence.agent_dao, "list_for_creator", owned)
    monkeypatch.setattr(fence.agent_manager, "_agent_dir", lambda _id: tmp_path / str(agent.id))
    monkeypatch.setattr(fence, "assert_no_active_run", allowed)
    monkeypatch.setattr(fence, "current_time", lambda: next(clock))
    with (locks / f"{agent.id}.lock").open("a+b") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # When final approval tries to fence the workspace.
        with anyio.fail_after(1), pytest.raises(HTTPException) as raised:
            await fence.fence_owned_agents(user)
    # Then the caller gets an explicit retryable conflict, not an endless wait.
    assert raised.value.status_code == 409
    assert raised.value.detail == {"code": "transfer_workspace_busy", "retryable": True}
