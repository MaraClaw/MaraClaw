import base64
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import clawbook
from app.core.domain.users import UserRole
from app.dao.clawbook_dao import ClawbookAdminCommentRow, ClawbookAdminPostRow, ClawbookPosition
from app.records.clawbook import ClawbookCommentRecord, ClawbookPostRecord
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.services.clawbook_cursor import ClawbookCursor

TIMESTAMP = datetime(2030, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
POSTS = "/api/clawbook/admin/posts/page"


@asynccontextmanager
async def _serving(actor: UserRecord) -> AsyncGenerator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(clawbook.router)
    app.dependency_overrides[clawbook.get_current_user] = lambda: actor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


def _org_admin() -> UserRecord:
    return UserRecord(id=uuid4(), tenant_id=uuid4(), role=UserRole.ORG_ADMIN)


def _post(tenant_id: UUID | None) -> ClawbookPostRecord:
    return ClawbookPostRecord(
        id=uuid4(),
        author_id=uuid4(),
        author_type="agent",
        author_name="Private agent",
        content="Update",
        tenant_id=tenant_id,
        created_at=TIMESTAMP,
    )


def _comment(post_id: UUID) -> ClawbookCommentRecord:
    return ClawbookCommentRecord(
        id=uuid4(),
        post_id=post_id,
        author_id=uuid4(),
        author_type="agent",
        author_name="Private agent",
        content="Reply",
        created_at=TIMESTAMP,
    )


@pytest.mark.parametrize(
    "path", [POSTS, f"/api/clawbook/admin/posts/{uuid4()}/comments/page"], ids=["posts", "comments"]
)
@pytest.mark.parametrize(
    "actor",
    [
        UserRecord(id=uuid4(), tenant_id=uuid4(), role=UserRole.MEMBER),
        UserRecord(id=uuid4(), tenant_id=uuid4(), role=UserRole.AGENT_ADMIN),
        UserRecord(id=uuid4(), tenant_id=uuid4(), role=UserRole.PLATFORM_ADMIN),
        UserRecord(id=uuid4(), tenant_id=None, role=UserRole.PLATFORM_ADMIN),
        UserRecord(id=uuid4(), tenant_id=None, role=UserRole.ORG_ADMIN),
        UserRecord(
            id=uuid4(),
            tenant_id=uuid4(),
            role=UserRole.ORG_ADMIN,
            identity=IdentityRecord(id=uuid4(), is_platform_admin=True),
        ),
    ],
    ids=[
        "member",
        "agent_admin",
        "platform_admin",
        "tenantless_platform_admin",
        "tenantless_org_admin",
        "identity_admin",
    ],
)
async def test_admin_routes_refuse_all_but_a_tenant_org_admin_before_the_cursor_or_a_read(monkeypatch, actor, path):
    guards = [AsyncMock(side_effect=AssertionError("a refused caller reached the database")) for _ in range(3)]
    monkeypatch.setattr(clawbook.clawbook_post_dao, "list_admin_feed_page", guards[0])
    monkeypatch.setattr(clawbook.clawbook_post_dao, "get_post", guards[1])
    monkeypatch.setattr(clawbook.clawbook_comment_dao, "list_admin_comments_page", guards[2])

    async with _serving(actor) as client:
        response = await client.get(path, params={"cursor": "%%%"})

    assert response.status_code == 403
    assert response.json() == {"detail": "Organization admin access is required"}
    for guard in guards:
        guard.assert_not_awaited()


@pytest.mark.parametrize("query", [{"limit": 0}, {"limit": 101}, {"cursor": "x" * 2049}])
async def test_admin_posts_page_rejects_unbounded_parameters(monkeypatch, query):
    read = AsyncMock()
    monkeypatch.setattr(clawbook.clawbook_post_dao, "list_admin_feed_page", read)

    async with _serving(_org_admin()) as client:
        response = await client.get(POSTS, params=query)

    assert response.status_code == 422
    read.assert_not_awaited()


async def test_admin_posts_page_flags_hidden_posts_and_binds_its_cursor_to_the_admin_scope(monkeypatch):
    actor = _org_admin()
    hidden, shown = _post(actor.tenant_id), replace(_post(actor.tenant_id), author_type="human")
    read = AsyncMock(side_effect=[[ClawbookAdminPostRow(hidden, True), ClawbookAdminPostRow(shown, False)], []])
    member_read = AsyncMock()
    monkeypatch.setattr(clawbook.clawbook_post_dao, "list_admin_feed_page", read)
    monkeypatch.setattr(clawbook.clawbook_post_dao, "list_feed_page", member_read)

    async with _serving(actor) as client:
        first = await client.get(POSTS, params={"limit": 1, "tenant_id": str(uuid4())})
        cursor = first.json()["next_cursor"]
        second = await client.get(POSTS, params={"limit": 1, "cursor": cursor})
        member_reuse = await client.get("/api/clawbook/posts/page", params={"cursor": cursor})

    assert first.status_code == 200
    assert [(item["id"], item["hidden_from_members"]) for item in first.json()["items"]] == [(str(hidden.id), True)]
    assert cursor is not None
    assert second.json() == {"items": [], "next_cursor": None}
    assert read.await_args_list[0].kwargs == {"tenant_id": actor.tenant_id, "before": None, "limit": 2}
    assert read.await_args_list[1].kwargs["before"] == ClawbookPosition(created_at=TIMESTAMP, id=hidden.id)
    assert member_reuse.status_code == 400
    member_read.assert_not_awaited()


async def test_admin_comments_page_includes_hidden_authors_oldest_first_and_binds_its_cursor(monkeypatch):
    actor = _org_admin()
    post = _post(actor.tenant_id)
    hidden, shown = _comment(post.id), replace(_comment(post.id), author_type="human")
    read = AsyncMock(side_effect=[[ClawbookAdminCommentRow(hidden, True), ClawbookAdminCommentRow(shown, False)], []])
    member_read = AsyncMock()
    monkeypatch.setattr(clawbook.clawbook_post_dao, "get_post", AsyncMock(return_value=post))
    monkeypatch.setattr(clawbook.clawbook_comment_dao, "list_admin_comments_page", read)
    monkeypatch.setattr(clawbook.clawbook_comment_dao, "list_comments_for_post", member_read)
    path = f"/api/clawbook/admin/posts/{post.id}/comments/page"

    async with _serving(actor) as client:
        first = await client.get(path, params={"limit": 1})
        cursor = first.json()["next_cursor"]
        second = await client.get(path, params={"limit": 1, "cursor": cursor})
        post_move = await client.get(f"/api/clawbook/admin/posts/{uuid4()}/comments/page", params={"cursor": cursor})

    assert first.status_code == 200
    assert [(item["id"], item["hidden_from_members"]) for item in first.json()["items"]] == [(str(hidden.id), True)]
    assert cursor is not None
    assert second.json() == {"items": [], "next_cursor": None}
    assert read.await_args_list[0].args == (post.id,)
    assert read.await_args_list[0].kwargs == {"after": None, "limit": 2}
    assert read.await_args_list[1].kwargs["after"] == ClawbookPosition(created_at=TIMESTAMP, id=hidden.id)
    assert post_move.status_code == 400
    member_read.assert_not_awaited()


@pytest.mark.parametrize("found", ["missing", "foreign", "tenantless"])
async def test_admin_comments_page_hides_missing_foreign_and_tenantless_posts(monkeypatch, found):
    post = {"missing": None, "foreign": _post(uuid4()), "tenantless": _post(None)}[found]
    read = AsyncMock()
    monkeypatch.setattr(clawbook.clawbook_post_dao, "get_post", AsyncMock(return_value=post))
    monkeypatch.setattr(clawbook.clawbook_comment_dao, "list_admin_comments_page", read)

    async with _serving(_org_admin()) as client:
        response = await client.get(f"/api/clawbook/admin/posts/{uuid4()}/comments/page")

    assert response.status_code == 404
    assert response.json() == {"detail": "Post not found"}
    read.assert_not_awaited()


@pytest.mark.parametrize("route", ["posts", "comments"])
async def test_admin_routes_reject_member_scope_cursors_before_reading(monkeypatch, route):
    actor = _org_admin()
    post = _post(actor.tenant_id)
    reads = [AsyncMock(), AsyncMock()]
    monkeypatch.setattr(clawbook.clawbook_post_dao, "get_post", AsyncMock(return_value=post))
    monkeypatch.setattr(clawbook.clawbook_post_dao, "list_admin_feed_page", reads[0])
    monkeypatch.setattr(clawbook.clawbook_comment_dao, "list_admin_comments_page", reads[1])
    member_cursor = ClawbookCursor(
        tenant_id=actor.tenant_id, post_id=None if route == "posts" else post.id, created_at=TIMESTAMP, id=post.id
    ).encode()
    path = POSTS if route == "posts" else f"/api/clawbook/admin/posts/{post.id}/comments/page"

    async with _serving(actor) as client:
        response = await client.get(path, params={"cursor": member_cursor})

    assert response.status_code == 400
    for read in reads:
        read.assert_not_awaited()


async def test_member_cursors_issued_before_the_scope_field_still_decode(monkeypatch):
    member = UserRecord(id=uuid4(), tenant_id=uuid4(), role=UserRole.MEMBER)
    boundary = ClawbookCursor(tenant_id=member.tenant_id, post_id=None, created_at=TIMESTAMP, id=uuid4())
    legacy_json = boundary.model_dump_json(exclude={"scope"})
    legacy = base64.urlsafe_b64encode(legacy_json.encode()).decode().rstrip("=")
    read = AsyncMock(return_value=[])
    monkeypatch.setattr(clawbook.clawbook_post_dao, "list_feed_page", read)

    async with _serving(member) as client:
        response = await client.get("/api/clawbook/posts/page", params={"cursor": legacy})

    assert "scope" not in legacy_json
    assert response.status_code == 200
    assert read.await_args is not None
    assert read.await_args.kwargs["before"] == ClawbookPosition(created_at=TIMESTAMP, id=boundary.id)
