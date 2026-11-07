from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import clawbook
from app.core.domain.users import UserRole
from app.records.agent import AgentRecord
from app.records.clawbook import ClawbookCommentRecord, ClawbookPostRecord
from app.records.user import UserRecord
from app.services import notification_service
from app.services.clawbook_cursor import ClawbookCursor


@pytest.fixture
def actor() -> UserRecord:
    return UserRecord(id=uuid4(), tenant_id=uuid4(), role=UserRole.MEMBER, display_name="Session member")


@pytest.fixture
async def client(actor: UserRecord) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(clawbook.router)
    app.dependency_overrides[clawbook.get_current_user] = lambda: actor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def post(actor: UserRecord) -> ClawbookPostRecord:
    return ClawbookPostRecord(
        id=uuid4(),
        author_id=uuid4(),
        author_type="human",
        author_name="Post owner",
        content="Update",
        tenant_id=actor.tenant_id,
        created_at=datetime(2030, 1, 2, tzinfo=UTC),
    )


@pytest.mark.parametrize("operation", ["post", "comment"])
async def test_human_writes_and_notifications_ignore_spoofed_identity(client, actor, post, monkeypatch, operation):
    spoofed_id = uuid4()
    mentioned = UserRecord(id=spoofed_id, display_name="Colleague", tenant_id=actor.tenant_id)
    prior_agent = uuid4()
    created = AsyncMock(
        return_value=post
        if operation == "post"
        else ClawbookCommentRecord(
            id=uuid4(),
            post_id=post.id,
            author_id=actor.id,
            author_type="human",
            author_name=actor.display_name,
            content="@Colleague hello",
            created_at=post.created_at,
        )
    )
    monkeypatch.setattr(clawbook.clawbook_post_dao, "create_post", created)
    monkeypatch.setattr(clawbook.clawbook_comment_dao, "create_comment", created)
    monkeypatch.setattr(clawbook.clawbook_post_dao, "get_post", AsyncMock(return_value=post))
    monkeypatch.setattr(clawbook.clawbook_post_dao, "increment_comments_count", AsyncMock())
    monkeypatch.setattr(
        clawbook.clawbook_comment_dao, "list_distinct_comment_authors", AsyncMock(return_value=[(prior_agent, "agent")])
    )
    monkeypatch.setattr(clawbook.agent_dao, "list_for_tenant", AsyncMock(return_value=[]))
    monkeypatch.setattr(clawbook.user_dao, "list_active_for_tenant", AsyncMock(return_value=[mentioned]))
    notify = AsyncMock()
    monkeypatch.setattr(notification_service, "send_notification", notify)
    endpoint = "/api/clawbook/posts" if operation == "post" else f"/api/clawbook/posts/{post.id}/comments"

    response = await client.post(
        endpoint,
        json={
            "content": "@Colleague hello",
            "author_id": str(spoofed_id),
            "author_name": "Forged colleague",
            "author_type": "human",
            "tenant_id": str(uuid4()),
        },
    )

    assert response.status_code == 200
    assert created.await_args is not None
    written = created.await_args.args[0]
    assert (written["author_id"], written["author_type"], written["author_name"]) == (
        actor.id,
        "human",
        actor.display_name,
    )
    assert {call.kwargs["sender_name"] for call in notify.await_args_list} == {actor.display_name}
    assert all(call.kwargs["title"].startswith(actor.display_name) for call in notify.await_args_list)
    expected_recipients = {spoofed_id} if operation == "post" else {post.author_id, prior_agent, spoofed_id}
    assert {
        call.kwargs.get("user_id", call.kwargs.get("agent_id")) for call in notify.await_args_list
    } == expected_recipients
    if operation == "post":
        assert written["tenant_id"] == actor.tenant_id


async def test_post_content_accepts_1000_characters_and_rejects_1001_before_writing(client, post, monkeypatch):
    created = AsyncMock(return_value=post)
    monkeypatch.setattr(clawbook.clawbook_post_dao, "create_post", created)

    full = "x" * 1000
    accepted = await client.post("/api/clawbook/posts", json={"content": full})
    rejected = await client.post("/api/clawbook/posts", json={"content": full + "x"})

    assert accepted.status_code == 200
    created.assert_awaited_once()
    assert created.await_args is not None
    assert created.await_args.args[0]["content"] == full
    assert rejected.status_code == 422
    assert created.await_count == 1


async def test_like_ignores_spoofed_human_id(client, actor, post, monkeypatch):
    monkeypatch.setattr(clawbook.clawbook_post_dao, "get_post", AsyncMock(return_value=post))
    toggle = AsyncMock(return_value=True)
    monkeypatch.setattr(clawbook.clawbook_like_dao, "toggle", toggle, raising=False)

    response = await client.post(f"/api/clawbook/posts/{post.id}/like", params={"author_id": str(uuid4())})

    assert response.status_code == 200
    assert response.json() == {"liked": True}
    toggle.assert_awaited_once_with(post.id, actor.id, "human")


@pytest.mark.parametrize("operation", ["post", "comment", "like"])
@pytest.mark.parametrize("access", ["use", "manage"])
async def test_rest_agent_authorship_requires_manage_and_uses_agent_name(
    client, actor, post, monkeypatch, operation, access
):
    agent = AgentRecord(id=uuid4(), name="Canonical agent", tenant_id=actor.tenant_id, creator_id=actor.id)
    monkeypatch.setattr(clawbook.agent_dao, "get", AsyncMock(return_value=agent))
    check = AsyncMock(return_value=(agent, access))
    monkeypatch.setattr(clawbook, "check_agent_access", check, raising=False)
    monkeypatch.setattr(clawbook.clawbook_post_dao, "get_post", AsyncMock(return_value=post))
    write = AsyncMock(return_value=post)
    comment_write = AsyncMock(
        return_value=ClawbookCommentRecord(
            id=uuid4(),
            post_id=post.id,
            author_id=agent.id,
            author_type="agent",
            author_name=agent.name,
            content="hello",
            created_at=post.created_at,
        )
    )
    monkeypatch.setattr(clawbook.clawbook_post_dao, "create_post", write)
    monkeypatch.setattr(clawbook.clawbook_comment_dao, "create_comment", comment_write)
    monkeypatch.setattr(clawbook.clawbook_post_dao, "increment_comments_count", AsyncMock())
    monkeypatch.setattr(clawbook.clawbook_comment_dao, "list_distinct_comment_authors", AsyncMock(return_value=[]))
    monkeypatch.setattr(notification_service, "send_notification", AsyncMock())
    toggle = AsyncMock(return_value=True)
    monkeypatch.setattr(clawbook.clawbook_like_dao, "toggle", toggle, raising=False)
    author = {"author_id": str(agent.id), "author_type": "agent", "author_name": "Forged name"}
    endpoint = (
        "/api/clawbook/posts"
        if operation == "post"
        else f"/api/clawbook/posts/{post.id}/{'comments' if operation == 'comment' else 'like'}"
    )

    response = await client.post(
        endpoint,
        params=author if operation == "like" else None,
        json=None if operation == "like" else {"content": "hello", **author},
    )

    assert response.status_code == (200 if access == "manage" else 403)
    check.assert_awaited_once_with(actor, agent.id)
    if access == "use":
        write.assert_not_awaited()
        comment_write.assert_not_awaited()
        toggle.assert_not_awaited()
    elif operation == "post":
        assert write.await_args is not None
        assert write.await_args.args[0]["author_name"] == agent.name
    elif operation == "comment":
        assert comment_write.await_args is not None
        assert comment_write.await_args.args[0]["author_name"] == agent.name
    else:
        toggle.assert_awaited_once_with(post.id, agent.id, "agent")


@pytest.mark.parametrize("operation", ["post", "comment", "like"])
@pytest.mark.parametrize("author_type", ["system", "", "Human"])
async def test_rest_rejects_unknown_author_type(client, post, operation, author_type):
    author = {"author_id": str(uuid4()), "author_type": author_type, "author_name": "Spoofed"}
    endpoint = (
        "/api/clawbook/posts"
        if operation == "post"
        else f"/api/clawbook/posts/{post.id}/{'comments' if operation == 'comment' else 'like'}"
    )

    response = await client.post(
        endpoint,
        params=author if operation == "like" else None,
        json=None if operation == "like" else {"content": "hello", **author},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("operation", ["post", "comment", "like"])
async def test_agent_authorship_requires_an_agent_id(client, post, operation):
    endpoint = (
        "/api/clawbook/posts"
        if operation == "post"
        else f"/api/clawbook/posts/{post.id}/{'comments' if operation == 'comment' else 'like'}"
    )
    response = await client.post(
        endpoint,
        params={"author_type": "agent"} if operation == "like" else None,
        json=None if operation == "like" else {"content": "hello", "author_type": "agent"},
    )
    assert response.status_code == 422


async def test_feed_page_preserves_boundary_precision_and_uses_effective_tenant(client, actor, post, monkeypatch):
    timestamp = datetime(2030, 1, 2, 3, 4, 5, 123456, tzinfo=timezone(timedelta(hours=7)))
    boundary = replace(post, created_at=timestamp)
    read = AsyncMock(side_effect=[[boundary, replace(post, id=uuid4())], []])
    monkeypatch.setattr(clawbook.clawbook_post_dao, "list_feed_page", read)

    first = await client.get("/api/clawbook/posts/page", params={"limit": 1, "tenant_id": str(uuid4())})
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    assert [item["id"] for item in first.json()["items"]] == [str(post.id)]
    second = await client.get("/api/clawbook/posts/page", params={"limit": 1, "cursor": cursor})

    assert second.status_code == 200
    assert second.json() == {"items": [], "next_cursor": None}
    assert read.await_args_list[0].kwargs["tenant_id"] == actor.tenant_id
    position = read.await_args_list[1].kwargs["before"]
    assert (position.created_at, position.id) == (timestamp, post.id)
    assert position.created_at.microsecond == 123456
    assert position.created_at.utcoffset() == timedelta(hours=7)
    assert read.await_args_list[0].kwargs["limit"] == 2


@pytest.mark.parametrize("kind", ["malformed", "tenant", "scope", "naive", "version"])
async def test_feed_rejects_invalid_or_foreign_cursors_before_reading(client, actor, post, monkeypatch, kind):
    read = AsyncMock()
    monkeypatch.setattr(clawbook.clawbook_post_dao, "list_feed_page", read)
    cursor = ClawbookCursor(tenant_id=actor.tenant_id, post_id=None, created_at=post.created_at, id=post.id)
    raw = "%%%"
    if kind == "tenant":
        raw = cursor.model_copy(update={"tenant_id": uuid4()}).encode()
    elif kind == "scope":
        raw = cursor.model_copy(update={"post_id": post.id}).encode()
    elif kind == "naive":
        raw = cursor.model_copy(update={"created_at": post.created_at.replace(tzinfo=None)}).encode()
    elif kind == "version":
        raw = cursor.model_copy(update={"version": 2}).encode()

    response = await client.get("/api/clawbook/posts/page", params={"cursor": raw})

    assert response.status_code == 400
    read.assert_not_awaited()


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "offset=10001"])
async def test_legacy_feed_rejects_unbounded_parameters(client, query):
    response = await client.get(f"/api/clawbook/posts?{query}")
    assert response.status_code == 422


async def test_legacy_feed_remains_a_list(client, post, monkeypatch):
    monkeypatch.setattr(clawbook.clawbook_post_dao, "list_feed", AsyncMock(return_value=[post]))
    response = await client.get("/api/clawbook/posts")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert response.json()[0]["id"] == str(post.id)


async def test_detail_bounds_comments_and_continuation_is_bound_to_post(client, actor, post, monkeypatch):
    comments = [
        ClawbookCommentRecord(
            id=uuid4(),
            post_id=post.id,
            author_id=actor.id,
            author_type="human",
            author_name="Member",
            content=str(index),
            created_at=post.created_at,
        )
        for index in range(31)
    ]
    read = AsyncMock(side_effect=[comments, [comments[-1]]])
    monkeypatch.setattr(clawbook.clawbook_post_dao, "get_post", AsyncMock(return_value=post))
    monkeypatch.setattr(clawbook.clawbook_comment_dao, "list_comments_for_post", read)
    detail = await client.get(f"/api/clawbook/posts/{post.id}")
    assert detail.status_code == 200
    assert len(detail.json()["comments"]) == 30
    cursor = detail.json()["next_cursor"]

    continuation = await client.get(f"/api/clawbook/posts/{post.id}/comments/page", params={"cursor": cursor})
    assert continuation.status_code == 200
    assert [item["id"] for item in continuation.json()["items"]] == [str(comments[-1].id)]
    assert continuation.json()["next_cursor"] is None
    assert read.await_args_list[0].kwargs == {"limit": 31, "visible_only": True, "after": None}
    other_post = replace(post, id=uuid4())
    monkeypatch.setattr(clawbook.clawbook_post_dao, "get_post", AsyncMock(return_value=other_post))
    foreign = await client.get(f"/api/clawbook/posts/{other_post.id}/comments/page", params={"cursor": cursor})
    assert foreign.status_code == 400


async def test_comment_page_hides_foreign_tenant_posts(client, post, monkeypatch):
    monkeypatch.setattr(
        clawbook.clawbook_post_dao, "get_post", AsyncMock(return_value=replace(post, tenant_id=uuid4()))
    )
    read = AsyncMock()
    monkeypatch.setattr(clawbook.clawbook_comment_dao, "list_comments_for_post", read)
    response = await client.get(f"/api/clawbook/posts/{post.id}/comments/page")
    assert response.status_code == 404
    read.assert_not_awaited()
