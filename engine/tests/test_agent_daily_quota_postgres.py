import asyncio
import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.dao.agent_dao import AgentDAO
from app.db.connection import DbConnection


@pytest.fixture
async def quota_db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[DbConnection, AgentDAO]]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for isolated PostgreSQL QA")
    schema = f"agent_quota_qa_{uuid4().hex}"
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            await db.execute(
                "CREATE TABLE agents (id UUID PRIMARY KEY, name TEXT NOT NULL, creator_id UUID NOT NULL, "
                "llm_calls_today INTEGER NOT NULL DEFAULT 0, max_llm_calls_per_day INTEGER NOT NULL DEFAULT 1000, "
                "llm_calls_reset_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            isolated = make_conninfo(conninfo, options=f"-csearch_path={schema}")
            dao = AgentDAO()
            monkeypatch.setattr(
                dao,
                "columns",
                (
                    "id",
                    "name",
                    "creator_id",
                    "llm_calls_today",
                    "max_llm_calls_per_day",
                    "llm_calls_reset_at",
                    "updated_at",
                ),
            )

            @asynccontextmanager
            async def separate_session() -> AsyncGenerator[DbConnection]:
                async with await AsyncConnection[DictRow].connect(
                    isolated, autocommit=True, row_factory=dict_row
                ) as separate:
                    connection = DbConnection(separate)
                    await connection.execute(
                        "SELECT set_config('TimeZone', CASE "
                        "WHEN EXTRACT(HOUR FROM NOW() AT TIME ZONE 'UTC') < 12 "
                        "THEN 'Etc/GMT-12' ELSE 'Etc/GMT+12' END, false)"
                    )
                    yield connection

            monkeypatch.setattr(dao, "session", separate_session)
            yield db, dao
        finally:
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.parametrize("reset", ["today", "yesterday", "null"])
async def test_concurrent_daily_charges_honor_cap_and_utc_rollover(quota_db, reset: str) -> None:
    # Given: a full counter in a non-UTC database session, including rollover boundaries.
    db, dao = quota_db
    agent_id = uuid4()
    await db.execute(
        "INSERT INTO agents (id, name, creator_id, llm_calls_today, max_llm_calls_per_day, llm_calls_reset_at) "
        "VALUES (%(id)s, 'quota', %(creator)s, 7, 7, CASE %(reset)s "
        "WHEN 'today' THEN date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' "
        "WHEN 'yesterday' THEN (date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') "
        "- interval '1 microsecond' ELSE NULL END)",
        {"id": agent_id, "creator": uuid4(), "reset": reset},
    )

    # When: independent committed connections race for the remaining daily budget.
    results = await asyncio.gather(*(dao.consume_llm_call(agent_id) for _ in range(24)))

    # Then: no more than seven requests win, and an old/null UTC day starts from zero.
    accepted = sorted(result.llm_calls_today for result in results if result is not None)
    assert accepted == ([] if reset == "today" else list(range(1, 8)))
    assert await db.fetchval("SELECT llm_calls_today FROM agents WHERE id = %(id)s", {"id": agent_id}) == 7
    if accepted:
        assert (
            await db.fetchval(
                "SELECT (llm_calls_reset_at AT TIME ZONE 'UTC')::date = (NOW() AT TIME ZONE 'UTC')::date "
                "FROM agents WHERE id = %(id)s",
                {"id": agent_id},
            )
            is True
        )


async def test_zero_daily_limit_is_unlimited_and_still_counts_concurrent_attempts(quota_db) -> None:
    # Given: an unlimited agent with an existing count on the current UTC day.
    db, dao = quota_db
    agent_id = uuid4()
    await db.execute(
        "INSERT INTO agents (id, name, creator_id, llm_calls_today, max_llm_calls_per_day, llm_calls_reset_at) "
        "VALUES (%(id)s, 'unlimited', %(creator)s, 5000, 0, NOW())",
        {"id": agent_id, "creator": uuid4()},
    )

    # When: more provider attempts arrive concurrently.
    results = await asyncio.gather(*(dao.consume_llm_call(agent_id) for _ in range(24)))

    # Then: zero never caps requests, and every admitted attempt increments exactly once.
    assert sorted(result.llm_calls_today for result in results if result is not None) == list(range(5001, 5025))
    assert await db.fetchval("SELECT llm_calls_today FROM agents WHERE id = %(id)s", {"id": agent_id}) == 5024


async def test_missing_agent_cannot_consume_daily_quota(quota_db) -> None:
    # Given: an agent id with no persisted row.
    _db, dao = quota_db
    # When: a dispatch tries to charge that id.
    result = await dao.consume_llm_call(uuid4())
    # Then: no request is admitted.
    assert result is None
