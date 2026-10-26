import asyncio
from unittest.mock import AsyncMock

import pytest
from psycopg import OperationalError
from psycopg_pool import PoolTimeout

import test_agent_setup
from app.db.errors import DbError
from app.services import agent_setup

setup_case = test_agent_setup.setup_case


@pytest.fixture(autouse=True)
def optional_queue(monkeypatch):
    monkeypatch.setattr(agent_setup.agent_setup_mcp_dao, "claim", AsyncMock(return_value=None))


async def test_worker_services_required_and_optional_queues_fairly(monkeypatch):
    required, optional = object(), object()
    order = []

    async def run_required(_claim):
        order.append("required")

    async def run_optional(_claim):
        order.append("optional")

    monkeypatch.setattr(
        agent_setup.agent_setup_dao, "claim", AsyncMock(side_effect=[required, required, asyncio.CancelledError()])
    )
    monkeypatch.setattr(agent_setup.agent_setup_mcp_dao, "claim", AsyncMock(return_value=optional))
    monkeypatch.setattr(agent_setup, "run_agent_setup", run_required)
    monkeypatch.setattr(agent_setup, "run_agent_setup_mcp", run_optional)
    with pytest.raises(asyncio.CancelledError):
        await agent_setup.start_agent_setup_worker()
    assert order == ["required", "optional", "required", "optional"]


@pytest.mark.parametrize("error", [DbError("offline", orig=OperationalError()), OSError(), PoolTimeout()])
async def test_worker_survives_expected_claim_failure(monkeypatch, error):
    # Given a transient claim failure followed by an idle queue.
    claim = AsyncMock(side_effect=[error, None])
    pauses = []

    async def pause(seconds):
        pauses.append(seconds)
        if len(pauses) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(agent_setup.agent_setup_dao, "claim", claim)
    monkeypatch.setattr(agent_setup.anyio, "sleep", pause)
    # When the worker continues until deliberately cancelled.
    with pytest.raises(asyncio.CancelledError):
        await agent_setup.start_agent_setup_worker()
    # Then another claim was attempted, with bounded positive backoff.
    assert claim.await_count == 2
    assert 0 < pauses[0] <= 5


@pytest.mark.parametrize("error", [TypeError("bug"), DbError("bug", orig=TypeError("bug"))])
async def test_worker_preserves_unexpected_claim_error(monkeypatch, error):
    # Given a programming failure rather than an infrastructure failure.
    monkeypatch.setattr(agent_setup.agent_setup_dao, "claim", AsyncMock(side_effect=error))
    # When the worker starts, then the error is not silently swallowed.
    with pytest.raises(type(error), match="bug"):
        await agent_setup.start_agent_setup_worker()


@pytest.mark.parametrize("failure", [None, "setup_failed"])
async def test_worker_retries_only_finish_after_transient_database_failure(setup_case, monkeypatch, failure):
    # Given a single claim whose completion write temporarily fails.
    store, _storage, _root = setup_case
    claim = AsyncMock(side_effect=[store.claim, asyncio.CancelledError()])
    execute = AsyncMock(side_effect=OSError("disk") if failure else None)
    finish = AsyncMock(side_effect=[DbError("offline", orig=OperationalError()), True])
    monkeypatch.setattr(agent_setup.agent_setup_dao, "claim", claim)
    monkeypatch.setattr(agent_setup, "_execute_with_renewal", execute)
    monkeypatch.setattr(agent_setup.agent_setup_dao, "finish", finish)
    monkeypatch.setattr(agent_setup.anyio, "sleep", AsyncMock())
    # When the worker completes the claim before cancellation.
    with pytest.raises(asyncio.CancelledError):
        await agent_setup.start_agent_setup_worker()
    # Then failed setup execution is not automatically replayed.
    assert execute.await_count == 1
    assert finish.await_count == 2
    assert finish.await_args is not None
    assert finish.await_args.args == (store.claim, failure)


async def test_completion_database_recovery_is_bounded_without_replaying_setup(setup_case, monkeypatch):
    # Given a successful replay whose completion database remains unavailable.
    store, _storage, _root = setup_case
    execute = AsyncMock()
    finish = AsyncMock(side_effect=DbError("offline", orig=OperationalError()))
    monkeypatch.setattr(agent_setup, "_execute_with_renewal", execute)
    monkeypatch.setattr(agent_setup.agent_setup_dao, "finish", finish)
    monkeypatch.setattr(agent_setup.anyio, "sleep", AsyncMock())
    # When the attempt exhausts its bounded completion writes.
    await agent_setup.run_agent_setup(store.claim)
    # Then it yields to lease recovery, never replaying inside this attempt.
    assert execute.await_count == 1
    assert finish.await_count == 3


async def test_cancellation_is_preserved_when_release_database_is_unavailable(setup_case, monkeypatch):
    # Given cancellation during setup and an unavailable release database.
    store, _storage, _root = setup_case
    monkeypatch.setattr(agent_setup, "_execute_with_renewal", AsyncMock(side_effect=asyncio.CancelledError()))
    monkeypatch.setattr(
        agent_setup.agent_setup_dao, "release", AsyncMock(side_effect=DbError("offline", orig=OperationalError()))
    )
    # When shutdown drains the setup attempt.
    with pytest.raises(asyncio.CancelledError):
        await agent_setup.run_agent_setup(store.claim)
    # Then cancellation remains visible and no terminal state is fabricated.
    assert store.terminal == []
