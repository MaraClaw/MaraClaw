import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
from fastapi import Depends, FastAPI
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.api import clawbook
from app.core.domain.users import UserRole
from app.dao.clawbook_dao import clawbook_comment_dao, clawbook_like_dao, clawbook_post_dao
from app.db import session as session_module
from app.db.connection import DbConnection, Params
from app.db.errors import LockNotAvailableError
from app.db.pool import close_pool, init_pool
from app.db.session import bind_crud_connection, connection_ctx
from app.records.clawbook import ClawbookPostRecord
from app.records.user import UserRecord
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


def _member(tenant: UUID, role: UserRole = UserRole.MEMBER) -> UserRecord:
    return UserRecord(id=uuid4(), tenant_id=tenant, role=role)


def _client(actor: UserRecord) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(clawbook.router, dependencies=[Depends(bind_crud_connection)])
    app.dependency_overrides[clawbook.get_current_user] = lambda: actor
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _footprint(db: DbConnection, post_id: UUID) -> dict[str, object]:
    row = await db.fetchone(
        "SELECT (SELECT COUNT(*) FROM clawbook_posts WHERE id = %(id)s) AS posts, "
        "(SELECT COUNT(*) FROM clawbook_likes WHERE post_id = %(id)s) AS likes, "
        "(SELECT COUNT(*) FROM clawbook_comments WHERE post_id = %(id)s) AS comments",
        {"id": post_id},
    )
    assert row is not None
    return row


async def _engaged_post(author: UserRecord, likes: int = 2, comments: int = 1) -> ClawbookPostRecord:
    post = await clawbook_post_dao.create_post(
        {
            "author_id": author.id,
            "author_type": "human",
            "author_name": "Member",
            "content": "Engaged",
            "tenant_id": author.tenant_id,
            "likes_count": likes,
            "comments_count": comments,
        }
    )
    for _ in range(likes):
        await clawbook_like_dao.create(obj_in={"post_id": post.id, "author_id": uuid4(), "author_type": "human"})
    for _ in range(comments):
        await clawbook_comment_dao.create_comment(
            {"post_id": post.id, "author_id": uuid4(), "author_type": "human", "author_name": "Fan", "content": "Hi"}
        )
    return post


async def test_delete_removes_a_liked_and_commented_post_over_http(clawbook_db: DbConnection) -> None:
    tenant = uuid4()
    author, fan = _member(tenant), _member(tenant)
    async with _client(author) as mine, _client(fan) as theirs:
        post_id = UUID((await mine.post("/api/clawbook/posts", json={"content": "Going away"})).json()["id"])
        assert (await theirs.post(f"/api/clawbook/posts/{post_id}/like")).json() == {"liked": True}
        assert (await mine.post(f"/api/clawbook/posts/{post_id}/like")).json() == {"liked": True}
        commented = await mine.post(f"/api/clawbook/posts/{post_id}/comments", json={"content": "First comment"})
        assert commented.status_code == 200
        assert await _footprint(clawbook_db, post_id) == {"posts": 1, "likes": 2, "comments": 1}

        deleted = await mine.delete(f"/api/clawbook/posts/{post_id}")
        repeated = await mine.delete(f"/api/clawbook/posts/{post_id}")

    assert (deleted.status_code, deleted.json()) == (200, {"deleted": True})
    assert await _footprint(clawbook_db, post_id) == {"posts": 0, "likes": 0, "comments": 0}
    assert repeated.status_code == 404


@pytest.mark.parametrize(("likes", "comments"), [(0, 0), (2, 0), (0, 1)])
async def test_delete_removes_posts_with_only_likes_only_comments_or_neither(
    clawbook_db: DbConnection, likes: int, comments: int
) -> None:
    author = _member(uuid4())
    post = await _engaged_post(author, likes, comments)

    async with _client(author) as client:
        response = await client.delete(f"/api/clawbook/posts/{post.id}")

    assert response.status_code == 200
    assert await _footprint(clawbook_db, post.id) == {"posts": 0, "likes": 0, "comments": 0}


@pytest.mark.parametrize(
    ("role", "relation", "status"),
    [
        (UserRole.MEMBER, "author", 200),
        (UserRole.ORG_ADMIN, "colleague", 200),
        (UserRole.PLATFORM_ADMIN, "stranger", 200),
        (UserRole.MEMBER, "colleague", 403),
        (UserRole.MEMBER, "stranger", 403),
        (UserRole.ORG_ADMIN, "stranger", 403),
    ],
)
async def test_delete_keeps_authorization_and_leaves_denied_posts_intact(
    clawbook_db: DbConnection, role: UserRole, relation: str, status: int
) -> None:
    tenant = uuid4()
    author = _member(tenant)
    post = await _engaged_post(author)
    actor = author if relation == "author" else _member(tenant if relation == "colleague" else uuid4(), role)

    async with _client(actor) as client:
        response = await client.delete(f"/api/clawbook/posts/{post.id}")

    assert response.status_code == status
    expected = {"posts": 0, "likes": 0, "comments": 0} if status == 200 else {"posts": 1, "likes": 2, "comments": 1}
    assert await _footprint(clawbook_db, post.id) == expected


async def test_delete_rolls_back_post_likes_and_comments_with_the_request(clawbook_db: DbConnection) -> None:
    post = await _engaged_post(_member(uuid4()))

    async def failed_request() -> None:
        async with connection_ctx():
            assert await clawbook_post_dao.delete(record_id=post.id) is not None
            raise RuntimeError("request failed")

    with pytest.raises(RuntimeError, match="request failed"):
        await failed_request()
    assert await _footprint(clawbook_db, post.id) == {"posts": 1, "likes": 2, "comments": 1}


async def test_delete_locks_the_post_against_new_likes_and_comments_before_removing_children(
    clawbook_db: DbConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    post = await _engaged_post(_member(uuid4()))
    locked_at_delete: list[bool] = []
    run = DbConnection.execute

    async def watching_execute(self: DbConnection, query: str, params: Params = None) -> None:
        if query.startswith("DELETE FROM clawbook_"):
            try:
                _ = await clawbook_db.fetchone(
                    "SELECT id FROM clawbook_posts WHERE id = %(id)s FOR KEY SHARE NOWAIT", {"id": post.id}
                )
            except LockNotAvailableError:
                locked_at_delete.append(True)
            else:
                locked_at_delete.append(False)
        await run(self, query, params)

    monkeypatch.setattr(DbConnection, "execute", watching_execute)

    assert await clawbook_post_dao.delete(record_id=post.id) is not None

    assert locked_at_delete
    assert all(locked_at_delete)
    assert await _footprint(clawbook_db, post.id) == {"posts": 0, "likes": 0, "comments": 0}


async def test_comment_and_like_racing_a_delete_find_the_post_gone(
    clawbook_db: DbConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid4()
    post = await _engaged_post(_member(tenant))
    read = clawbook.clawbook_post_dao.get_post
    reads: list[UUID] = []
    both_read = anyio.Event()
    deleted_uncommitted = anyio.Event()
    release = anyio.Event()
    statuses: dict[str, int] = {}

    async def read_then_report(post_id: UUID) -> ClawbookPostRecord | None:
        found = await read(post_id)
        reads.append(post_id)
        if len(reads) == 2:
            both_read.set()
        return found

    async def delete_and_hold() -> None:
        async with connection_ctx():
            assert await clawbook_post_dao.delete(record_id=post.id) is not None
            deleted_uncommitted.set()
            await release.wait()

    async def racer(action: str, body: dict[str, str] | None) -> None:
        async with _client(_member(tenant)) as client:
            statuses[action] = (await client.post(f"/api/clawbook/posts/{post.id}/{action}", json=body)).status_code

    monkeypatch.setattr(clawbook.clawbook_post_dao, "get_post", read_then_report)
    async with anyio.create_task_group() as group:
        group.start_soon(delete_and_hold)
        await deleted_uncommitted.wait()
        group.start_soon(racer, "comments", {"content": "Too late"})
        group.start_soon(racer, "like", None)
        await both_read.wait()
        release.set()

    assert statuses == {"comments": 404, "like": 404}
    assert await _footprint(clawbook_db, post.id) == {"posts": 0, "likes": 0, "comments": 0}
