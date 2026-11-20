from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, FastAPI

from app.api import clawbook
from app.core.domain.users import UserRole
from app.dao.clawbook_dao import clawbook_comment_dao, clawbook_post_dao
from app.db.connection import DbConnection
from app.db.session import bind_crud_connection
from app.records.user import UserRecord

pytest_plugins = ["test_clawbook_likes_postgres"]

TIMESTAMP = datetime(2030, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
POSTS = "/api/clawbook/admin/posts/page"


def _client(actor: UserRecord) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(clawbook.router, dependencies=[Depends(bind_crud_connection)])
    app.dependency_overrides[clawbook.get_current_user] = lambda: actor
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _org_admin(tenant: UUID) -> UserRecord:
    return UserRecord(id=uuid4(), tenant_id=tenant, role=UserRole.ORG_ADMIN)


async def _seed_agents(db: DbConnection, creator: UUID) -> dict[str, UUID]:
    await db.execute("ALTER TABLE agents DROP CONSTRAINT agents_creator_id_fkey")
    agents: dict[str, UUID] = {}
    for label, mode, system in [
        ("company", "company", False),
        ("private", "user", False),
        ("custom", "custom", False),
        ("system", "company", True),
    ]:
        agents[label] = uuid4()
        await db.execute(
            "INSERT INTO agents (id, name, role_description, creator_id, max_triggers, min_poll_interval_min, "
            "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes, is_system) "
            "VALUES (%(id)s, %(name)s, 'QA', %(creator)s, 1, 1, 1, %(mode)s, 'use', 60, %(system)s)",
            {"id": agents[label], "name": label, "creator": creator, "mode": mode, "system": system},
        )
    return agents


async def _post(
    number: int, tenant: UUID | None, author: UUID, author_type: str = "agent", created_at: datetime = TIMESTAMP
) -> None:
    _ = await clawbook_post_dao.create_post(
        {
            "id": UUID(int=number),
            "author_id": author,
            "author_type": author_type,
            "author_name": "Author",
            "content": f"Post {number}",
            "tenant_id": tenant,
            "created_at": created_at,
        }
    )


async def _comments(post_number: int, authors: list[tuple[str, UUID]]) -> None:
    for index, (author_type, author_id) in enumerate(authors):
        _ = await clawbook_comment_dao.create_comment(
            {
                "id": UUID(int=post_number * 100 + index),
                "post_id": UUID(int=post_number),
                "author_id": author_id,
                "author_type": author_type,
                "author_name": "Commenter",
                "content": str(index),
                "created_at": TIMESTAMP,
            }
        )


async def _collect(client: httpx.AsyncClient, path: str, limit: int) -> tuple[list[dict[str, object]], int]:
    items: list[dict[str, object]] = []
    cursor = None
    pages = 0
    while True:
        response = await client.get(path, params={"limit": limit, **({"cursor": cursor} if cursor else {})})
        assert response.status_code == 200
        items.extend(response.json()["items"])
        cursor = response.json()["next_cursor"]
        pages += 1
        if cursor is None:
            return items, pages


async def test_admin_feed_pages_every_tenant_post_and_flags_what_the_member_feed_omits(
    clawbook_db: DbConnection,
) -> None:
    tenant, other = uuid4(), uuid4()
    admin = _org_admin(tenant)
    agents = await _seed_agents(clawbook_db, admin.id)
    for number, author, author_type in [
        (10, admin.id, "human"),
        (20, agents["company"], "agent"),
        (30, agents["private"], "agent"),
        (40, agents["custom"], "agent"),
        (50, agents["system"], "agent"),
    ]:
        await _post(number, tenant, author, author_type)
    await _post(60, tenant, admin.id, "human", TIMESTAMP + timedelta(microseconds=1))
    await _post(70, other, agents["system"])
    await _post(80, other, admin.id, "human")
    await _post(90, None, agents["system"])

    async with _client(admin) as client:
        items, pages = await _collect(client, POSTS, limit=4)
        member_feed, _ = await _collect(client, "/api/clawbook/posts/page", limit=30)

    expected = [(60, False), (50, True), (40, True), (30, True), (20, False), (10, False)]
    assert pages == 2
    assert [(item["id"], item["hidden_from_members"]) for item in items] == [
        (str(UUID(int=number)), hidden) for number, hidden in expected
    ]
    assert [item["id"] for item in member_feed] == [str(UUID(int=number)) for number, hidden in expected if not hidden]
    assert set(items[0]) == {
        "id",
        "author_id",
        "author_type",
        "author_name",
        "content",
        "likes_count",
        "comments_count",
        "created_at",
        "hidden_from_members",
    }


async def test_admin_comments_page_includes_hidden_authors_and_refuses_foreign_posts(
    clawbook_db: DbConnection,
) -> None:
    tenant, other = uuid4(), uuid4()
    admin = _org_admin(tenant)
    agents = await _seed_agents(clawbook_db, admin.id)
    await _post(1, tenant, agents["system"])
    await _post(2, other, admin.id, "human")
    await _post(3, None, admin.id, "human")
    authors = [
        ("human", admin.id),
        ("agent", agents["private"]),
        ("agent", agents["company"]),
        ("agent", agents["system"]),
        ("human", admin.id),
    ]
    for number in (1, 2, 3):
        await _comments(number, authors)

    async with _client(admin) as client:
        items, pages = await _collect(client, f"/api/clawbook/admin/posts/{UUID(int=1)}/comments/page", limit=2)
        member = await client.get(f"/api/clawbook/posts/{UUID(int=1)}/comments/page")
        refused = [
            await client.get(f"/api/clawbook/admin/posts/{post_id}/comments/page")
            for post_id in (UUID(int=2), UUID(int=3), uuid4())
        ]

    assert pages == 3
    assert [(item["content"], item["hidden_from_members"]) for item in items] == [
        ("0", False),
        ("1", True),
        ("2", False),
        ("3", True),
        ("4", False),
    ]
    assert member.status_code == 404
    assert [(response.status_code, response.json()) for response in refused] == [
        (404, {"detail": "Post not found"})
    ] * 3


async def test_cursors_stay_bound_to_the_scope_that_issued_them(clawbook_db: DbConnection) -> None:
    tenant = uuid4()
    admin = _org_admin(tenant)
    for number in (1, 2, 3):
        await _post(number, tenant, admin.id, "human")
    await _comments(1, [("human", admin.id)] * 3)
    post_comments = f"/{UUID(int=1)}/comments/page"
    admin_paths = [POSTS, f"/api/clawbook/admin/posts{post_comments}"]
    member_paths = ["/api/clawbook/posts/page", f"/api/clawbook/posts{post_comments}"]

    async with _client(admin) as client:
        admin_cursors = [(await client.get(path, params={"limit": 1})).json()["next_cursor"] for path in admin_paths]
        member_cursors = [(await client.get(path, params={"limit": 1})).json()["next_cursor"] for path in member_paths]
        crossed = [
            await client.get(path, params={"cursor": cursor})
            for path, cursor in [
                *zip(admin_paths, member_cursors, strict=True),
                *zip(member_paths, admin_cursors, strict=True),
            ]
        ]
        own = [
            await client.get(path, params={"cursor": cursor})
            for path, cursor in [
                *zip(admin_paths, admin_cursors, strict=True),
                *zip(member_paths, member_cursors, strict=True),
            ]
        ]

    assert [response.status_code for response in crossed] == [400] * 4
    assert [response.status_code for response in own] == [200] * 4
