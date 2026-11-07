import os
from collections.abc import AsyncIterator
from uuid import uuid4

import anyio
import pytest
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.dao.clawbook_dao import clawbook_like_dao, clawbook_post_dao
from app.db import session as session_module
from app.db.connection import DbConnection
from app.db.pool import close_pool, init_pool
from app.db.session import connection_ctx
from app.scripts.bootstrap_db import SCHEMA_BASELINE, _statement_bodies


@pytest.fixture
async def clawbook_db() -> AsyncIterator[DbConnection]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for isolated PostgreSQL QA")
    schema = f"clawbook_likes_qa_{uuid4().hex}"
    token = session_module._conn_ctx.set(None)
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            for statement in _statement_bodies(SCHEMA_BASELINE.read_text()):
                await db.execute(statement)
            await init_pool(conninfo=make_conninfo(conninfo, options=f"-csearch_path={schema}"), min_size=4, max_size=4)
            yield db
        finally:
            await close_pool()
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')
            session_module._conn_ctx.reset(token)


@pytest.mark.parametrize("legacy_duplicates", [0, 2])
async def test_toggle_repairs_count_from_actual_rows(clawbook_db: DbConnection, legacy_duplicates: int) -> None:
    author_id = uuid4()
    post = await clawbook_post_dao.create_post(
        {"author_id": uuid4(), "author_type": "human", "author_name": "Member", "content": "QA", "likes_count": 99}
    )
    for liker in [uuid4(), *([author_id] * legacy_duplicates)]:
        await clawbook_like_dao.create(obj_in={"post_id": post.id, "author_id": liker, "author_type": "human"})
    # A baseline rerun must keep accepting the pre-existing duplicate rows.
    for statement in _statement_bodies(SCHEMA_BASELINE.read_text()):
        await clawbook_db.execute(statement)

    liked = await clawbook_like_dao.toggle(post.id, author_id, "human")

    expected = 2 if legacy_duplicates == 0 else 1
    assert liked is not None
    assert liked is (legacy_duplicates == 0)
    assert (
        await clawbook_db.fetchval("SELECT COUNT(*) FROM clawbook_likes WHERE post_id = %(id)s", {"id": post.id})
        == expected
    )
    assert (
        await clawbook_db.fetchval("SELECT likes_count FROM clawbook_posts WHERE id = %(id)s", {"id": post.id})
        == expected
    )
    assert await clawbook_db.fetchval(
        "SELECT COUNT(*) FROM clawbook_likes WHERE post_id = %(post)s AND author_id = %(author)s",
        {"post": post.id, "author": author_id},
    ) == int(liked)


@pytest.mark.parametrize("same_author", [True, False])
async def test_concurrent_toggles_preserve_parity_and_exact_count(clawbook_db: DbConnection, same_author: bool) -> None:
    author_id = uuid4()
    post = await clawbook_post_dao.create_post(
        {"author_id": uuid4(), "author_type": "human", "author_name": "Member", "content": "Concurrent QA"}
    )
    start = anyio.Event()
    results: list[bool | None] = []

    async def toggle() -> None:
        await start.wait()
        results.append(await clawbook_like_dao.toggle(post.id, author_id if same_author else uuid4(), "human"))

    async with anyio.create_task_group() as group:
        for _ in range(12):
            group.start_soon(toggle)
        start.set()

    expected = 0 if same_author else 12
    assert results.count(True) == (6 if same_author else 12)
    assert results.count(False) == (6 if same_author else 0)
    assert (
        await clawbook_db.fetchval("SELECT COUNT(*) FROM clawbook_likes WHERE post_id = %(id)s", {"id": post.id})
        == expected
    )
    assert (
        await clawbook_db.fetchval("SELECT likes_count FROM clawbook_posts WHERE id = %(id)s", {"id": post.id})
        == expected
    )


async def test_toggle_joins_request_transaction_and_missing_post_is_unavailable(clawbook_db: DbConnection) -> None:
    author_id = uuid4()
    post = await clawbook_post_dao.create_post(
        {"author_id": uuid4(), "author_type": "human", "author_name": "Member", "content": "Rollback QA"}
    )

    async def failed_request() -> None:
        async with connection_ctx():
            assert await clawbook_like_dao.toggle(post.id, author_id, "human") is True
            raise RuntimeError("request failed")

    with pytest.raises(RuntimeError, match="request failed"):
        await failed_request()
    assert (
        await clawbook_db.fetchval("SELECT COUNT(*) FROM clawbook_likes WHERE post_id = %(id)s", {"id": post.id}) == 0
    )
    assert await clawbook_db.fetchval("SELECT likes_count FROM clawbook_posts WHERE id = %(id)s", {"id": post.id}) == 0
    assert await clawbook_like_dao.toggle(uuid4(), author_id, "human") is None
