import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import DictRow, dict_row

from app.core.domain.tasks import TaskStatus
from app.dao import task_dao as exported_task_dao
from app.dao.task_dao import TaskDAO
from app.db.connection import DbConnection
from app.records.task import TaskClaim

AGENT = UUID(int=100)
TASK = UUID(int=101)


@pytest.fixture
async def database(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[DbConnection]:
    dsn = os.environ.get("MARACLAW_TEST_POSTGRES_DSN")
    if dsn is None or dsn == "":
        pytest.skip("Set MARACLAW_TEST_POSTGRES_DSN to run isolated PostgreSQL claim checks")
    schema = f"task_claim_{uuid4().hex}"
    async with await psycopg.AsyncConnection[DictRow].connect(dsn, autocommit=True, row_factory=dict_row) as raw:
        await raw.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        await raw.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        try:
            await raw.execute("""
                CREATE TABLE tasks (
                    id uuid PRIMARY KEY, agent_id uuid NOT NULL, status text NOT NULL DEFAULT 'pending',
                    claim_token uuid, lease_expires_at timestamptz, completed_at timestamptz,
                    updated_at timestamptz DEFAULT now(),
                    CHECK ((claim_token IS NULL) = (lease_expires_at IS NULL))
                );
                CREATE TABLE task_logs (
                    id uuid PRIMARY KEY, task_id uuid REFERENCES tasks(id), content text NOT NULL
                );
            """)
            await raw.execute("INSERT INTO tasks (id, agent_id) VALUES (%s, %s)", (TASK, AGENT))

            @asynccontextmanager
            async def session() -> AsyncGenerator[DbConnection]:
                async with await psycopg.AsyncConnection[DictRow].connect(dsn, row_factory=dict_row) as child:
                    await child.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                    yield DbConnection(child)

            import importlib

            module = importlib.import_module("app.dao.task_dao")
            monkeypatch.setattr(TaskDAO, "session", lambda _self: session())
            monkeypatch.setattr(module, "_committed", session)
            yield DbConnection(raw)
        finally:
            await raw.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


async def test_concurrent_claims_have_exactly_one_winner(database: DbConnection):
    del database
    ready = asyncio.Event()

    async def claim():
        await ready.wait()
        return await TaskDAO().claim(TASK, AGENT, lease_seconds=120)

    first, second = asyncio.create_task(claim()), asyncio.create_task(claim())
    ready.set()
    claims = await asyncio.gather(first, second)

    assert sum(item is not None for item in claims) == 1


async def test_stale_token_cannot_renew_complete_release_or_add_log(database: DbConnection):
    dao = exported_task_dao
    old = await dao.claim(TASK, AGENT, lease_seconds=120)
    assert old is not None
    await database.execute("UPDATE tasks SET lease_expires_at = clock_timestamp() - interval '1 second'")
    successor = await dao.claim(TASK, AGENT, lease_seconds=120)
    assert successor is not None

    assert not await dao.renew(old, lease_seconds=120)
    assert not await dao.complete(old, status=TaskStatus.DONE, log="stale completion")
    assert not await dao.complete(old, status=TaskStatus.PENDING, log="stale supervision")
    assert not await dao.release(old, log="stale failure")

    assert await database.fetchone("SELECT status, claim_token FROM tasks") == {
        "status": "doing",
        "claim_token": successor.token,
    }
    assert await database.fetchval("SELECT count(*) FROM task_logs") == 0


@pytest.mark.parametrize("status", [TaskStatus.DONE, TaskStatus.PENDING])
async def test_completion_commits_status_timestamp_and_log(database: DbConnection, status: TaskStatus):
    dao = TaskDAO()
    claim = await dao.claim(TASK, AGENT, lease_seconds=120)
    assert claim is not None

    assert await dao.complete(claim, status=status, log="successful run")

    row = await database.fetchone("SELECT status, completed_at, claim_token, lease_expires_at FROM tasks")
    assert row is not None
    assert row["status"] == status
    assert (row["completed_at"] is not None) == (status == TaskStatus.DONE)
    assert row["claim_token"] is None
    assert row["lease_expires_at"] is None
    assert await database.fetchval("SELECT content FROM task_logs") == "successful run"


async def test_recovery_clears_only_expired_lease_and_logs_once(database: DbConnection):
    dao = TaskDAO()
    old = await dao.claim(TASK, AGENT, lease_seconds=120)
    assert old is not None
    live_task = uuid4()
    await database.execute(
        "INSERT INTO tasks (id, agent_id, status, claim_token, lease_expires_at) "
        "VALUES (%(id)s, %(agent)s, 'doing', %(token)s, clock_timestamp() + interval '120 seconds')",
        {"id": live_task, "agent": AGENT, "token": uuid4()},
    )
    await database.execute(
        "UPDATE tasks SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE id = %(id)s",
        {"id": TASK},
    )

    assert await dao.recover_expired(limit=100, log="expired; not rerun") == [TASK]
    assert await dao.recover_expired(limit=100, log="duplicate") == []

    assert await database.fetchone("SELECT status, claim_token FROM tasks WHERE id = %(id)s", {"id": TASK}) == {
        "status": "pending",
        "claim_token": None,
    }
    assert await database.fetchval("SELECT status FROM tasks WHERE id = %(id)s", {"id": live_task}) == "doing"
    assert await database.fetchval("SELECT content FROM task_logs") == "expired; not rerun"
    assert not await dao.complete(old, status=TaskStatus.DONE, log="old result")


async def test_expired_owner_cannot_renew_or_publish_success(database: DbConnection):
    dao = TaskDAO()
    claim = await dao.claim(TASK, AGENT, lease_seconds=120)
    assert isinstance(claim, TaskClaim)
    await database.execute("UPDATE tasks SET lease_expires_at = clock_timestamp() - interval '1 second'")

    assert not await dao.renew(claim, lease_seconds=120)
    assert not await dao.complete(claim, status=TaskStatus.DONE, log="late todo")
    assert not await dao.complete(claim, status=TaskStatus.PENDING, log="late supervision")
    assert await database.fetchval("SELECT count(*) FROM task_logs") == 0
