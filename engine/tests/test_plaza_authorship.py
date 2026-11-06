from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import plaza
from app.core.domain.users import UserRole
from app.records.agent import AgentRecord
from app.records.plaza import PlazaCommentRecord, PlazaPostRecord
from app.records.user import UserRecord
from app.services import notification_service


@pytest.fixture
def actor() -> UserRecord:
    return UserRecord(id=uuid4(), tenant_id=uuid4(), role=UserRole.MEMBER, display_name="Session member")


@pytest.fixture
async def client(actor: UserRecord) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(plaza.router)
    app.dependency_overrides[plaza.get_current_user] = lambda: actor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def post(actor: UserRecord) -> PlazaPostRecord:
    return PlazaPostRecord(
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
        else PlazaCommentRecord(
            id=uuid4(),
            post_id=post.id,
            author_id=actor.id,
            author_type="human",
            author_name=actor.display_name,
            content="@Colleague hello",
            created_at=post.created_at,
        )
    )
    monkeypatch.setattr(plaza.plaza_post_dao, "create_post", created)
    monkeypatch.setattr(plaza.plaza_comment_dao, "create_comment", created)
    monkeypatch.setattr(plaza.plaza_post_dao, "get_post", AsyncMock(return_value=post))
    monkeypatch.setattr(plaza.plaza_post_dao, "increment_comments_count", AsyncMock())
    monkeypatch.setattr(
        plaza.plaza_comment_dao, "list_distinct_comment_authors", AsyncMock(return_value=[(prior_agent, "agent")])
    )
    monkeypatch.setattr(plaza.agent_dao, "list_for_tenant", AsyncMock(return_value=[]))
    monkeypatch.setattr(plaza.user_dao, "list_active_for_tenant", AsyncMock(return_value=[mentioned]))
    notify = AsyncMock()
    monkeypatch.setattr(notification_service, "send_notification", notify)
    endpoint = "/api/plaza/posts" if operation == "post" else f"/api/plaza/posts/{post.id}/comments"

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


async def test_like_ignores_spoofed_human_id(client, actor, post, monkeypatch):
    monkeypatch.setattr(plaza.plaza_post_dao, "get_post", AsyncMock(return_value=post))
    toggle = AsyncMock(return_value=True)
    monkeypatch.setattr(plaza.plaza_like_dao, "toggle", toggle, raising=False)

    response = await client.post(f"/api/plaza/posts/{post.id}/like", params={"author_id": str(uuid4())})

    assert response.status_code == 200
    assert response.json() == {"liked": True}
    toggle.assert_awaited_once_with(post.id, actor.id, "human")


@pytest.mark.parametrize("operation", ["post", "comment", "like"])
@pytest.mark.parametrize("access", ["use", "manage"])
async def test_rest_agent_authorship_requires_manage_and_uses_agent_name(
    client, actor, post, monkeypatch, operation, access
):
    agent = AgentRecord(id=uuid4(), name="Canonical agent", tenant_id=actor.tenant_id, creator_id=actor.id)
    monkeypatch.setattr(plaza.agent_dao, "get", AsyncMock(return_value=agent))
    check = AsyncMock(return_value=(agent, access))
    monkeypatch.setattr(plaza, "check_agent_access", check, raising=False)
    monkeypatch.setattr(plaza.plaza_post_dao, "get_post", AsyncMock(return_value=post))
    write = AsyncMock(return_value=post)
    comment_write = AsyncMock(
        return_value=PlazaCommentRecord(
            id=uuid4(),
            post_id=post.id,
            author_id=agent.id,
            author_type="agent",
            author_name=agent.name,
            content="hello",
            created_at=post.created_at,
        )
    )
    monkeypatch.setattr(plaza.plaza_post_dao, "create_post", write)
    monkeypatch.setattr(plaza.plaza_comment_dao, "create_comment", comment_write)
    monkeypatch.setattr(plaza.plaza_post_dao, "increment_comments_count", AsyncMock())
    monkeypatch.setattr(plaza.plaza_comment_dao, "list_distinct_comment_authors", AsyncMock(return_value=[]))
    monkeypatch.setattr(notification_service, "send_notification", AsyncMock())
    toggle = AsyncMock(return_value=True)
    monkeypatch.setattr(plaza.plaza_like_dao, "toggle", toggle, raising=False)
    author = {"author_id": str(agent.id), "author_type": "agent", "author_name": "Forged name"}
    endpoint = (
        "/api/plaza/posts"
        if operation == "post"
        else f"/api/plaza/posts/{post.id}/{'comments' if operation == 'comment' else 'like'}"
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
        "/api/plaza/posts"
        if operation == "post"
        else f"/api/plaza/posts/{post.id}/{'comments' if operation == 'comment' else 'like'}"
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
        "/api/plaza/posts"
        if operation == "post"
        else f"/api/plaza/posts/{post.id}/{'comments' if operation == 'comment' else 'like'}"
    )
    response = await client.post(
        endpoint,
        params={"author_type": "agent"} if operation == "like" else None,
        json=None if operation == "like" else {"content": "hello", "author_type": "agent"},
    )
    assert response.status_code == 422
