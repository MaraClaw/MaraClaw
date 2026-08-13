import importlib
import socket
import subprocess
import uuid
import webbrowser
from datetime import UTC, datetime
from inspect import signature
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import database
from app.services import agent_tools, notification_service
from app.services.sandbox import registry as sandbox_registry


def _unexpected_external(*_args, **_kwargs):
    raise AssertionError("test attempted an external call")


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    monkeypatch.setattr(database, "async_session", _unexpected_external)
    monkeypatch.setattr(socket, "create_connection", _unexpected_external)
    monkeypatch.setattr(socket.socket, "connect", _unexpected_external)
    monkeypatch.setattr(subprocess, "run", _unexpected_external)
    monkeypatch.setattr(subprocess, "Popen", _unexpected_external)
    monkeypatch.setattr(httpx, "AsyncClient", _unexpected_external)
    monkeypatch.setattr(webbrowser, "open", _unexpected_external)
    monkeypatch.setattr(sandbox_registry, "get_sandbox_backend", _unexpected_external)


@pytest.fixture
def plaza():
    return importlib.import_module("app.services.agent_tool_exec.plaza")


def _agent(agent_id, **overrides):
    attributes = {
        "id": agent_id,
        "name": "Ada",
        "tenant_id": uuid.uuid4(),
        "is_system": False,
        "access_mode": "company",
        "creator_id": None,
    }
    attributes.update(overrides)
    return SimpleNamespace(**attributes)


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (None, "Error: Agent not found."),
        (_agent(uuid.uuid4(), is_system=True), "System agents cannot access Plaza."),
        (_agent(uuid.uuid4(), access_mode="private"), "Only company-wide agents can access Plaza."),
    ],
)
async def test_get_new_posts_returns_access_guards(monkeypatch, plaza, agent, expected):
    monkeypatch.setattr(plaza.agent_dao, "get", AsyncMock(return_value=agent))

    result = await agent_tools._plaza_get_new_posts(uuid.uuid4(), {})

    assert result == expected


async def test_get_new_posts_returns_empty_feed_and_caps_limit(monkeypatch, plaza):
    agent_id = uuid.uuid4()
    agent = _agent(agent_id)
    monkeypatch.setattr(plaza.agent_dao, "get", AsyncMock(return_value=agent))
    list_posts = AsyncMock(return_value=[])
    monkeypatch.setattr(plaza.plaza_post_dao, "list_posts_recent", list_posts)

    result = await agent_tools._plaza_get_new_posts(agent_id, {"limit": 99})

    assert result == "📭 No posts in the plaza yet. Be the first to share something!"
    list_posts.assert_awaited_once_with(20, tenant_id=agent.tenant_id)


async def test_get_new_posts_formats_tenant_posts_and_comments(monkeypatch, plaza):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    post_id = uuid.uuid4()
    post = SimpleNamespace(
        id=post_id,
        author_type="agent",
        author_name="Ada",
        created_at=datetime(2026, 7, 10, 9, 30, tzinfo=UTC),
        content="Hello Plaza",
        likes_count=2,
        comments_count=1,
    )
    comment = SimpleNamespace(author_type="human", author_name="Mira", content="Welcome")
    monkeypatch.setattr(plaza.agent_dao, "get", AsyncMock(return_value=_agent(agent_id, tenant_id=tenant_id)))
    list_posts = AsyncMock(return_value=[post])
    list_comments = AsyncMock(return_value=[comment])
    monkeypatch.setattr(plaza.plaza_post_dao, "list_posts_recent", list_posts)
    monkeypatch.setattr(plaza.plaza_comment_dao, "list_comments_for_post", list_comments)

    result = await agent_tools._plaza_get_new_posts(agent_id, {"limit": 1})

    assert result == (
        "🏛️ Agent Plaza — Recent Posts:\n\n"
        f"🤖 **Ada** (07-10 09:30) [post_id: {post_id}]\n"
        "Hello Plaza\n❤️ 2  💬 1\n  └─ 👤 Mira: Welcome"
    )
    list_posts.assert_awaited_once_with(1, tenant_id=tenant_id)
    list_comments.assert_awaited_once_with(post_id, limit=5)


async def test_get_new_posts_truncates_database_failure(monkeypatch, plaza):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(plaza.agent_dao, "get", AsyncMock(side_effect=RuntimeError("x" * 250)))

    result = await agent_tools._plaza_get_new_posts(agent_id, {})

    assert result == f"❌ Failed to load plaza posts: {'x' * 200}"


async def test_create_post_rejects_blank_content():
    assert (
        await agent_tools._plaza_create_post(uuid.uuid4(), {"content": "  "}) == "Error: Post content cannot be empty."
    )


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (None, "Error: Agent not found."),
        (
            _agent(uuid.uuid4(), is_system=True),
            "System agents are not allowed to post to Plaza. Use send_platform_message to communicate with users directly.",
        ),
        (_agent(uuid.uuid4(), access_mode="private"), "Only company-wide agents are allowed to post to Plaza."),
    ],
)
async def test_create_post_returns_access_guards(monkeypatch, plaza, agent, expected):
    monkeypatch.setattr(plaza.agent_dao, "get", AsyncMock(return_value=agent))

    result = await agent_tools._plaza_create_post(uuid.uuid4(), {"content": "Hello"})

    assert result == expected


async def test_create_post_truncates_content_and_deduplicates_tenant_mentions(monkeypatch, plaza):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    mentioned_id = uuid.uuid4()
    post_id = uuid.uuid4()
    content = "@Bob @bob " + "x" * 600
    agent = _agent(agent_id, tenant_id=tenant_id)
    post = SimpleNamespace(id=post_id, content=content[:500])
    monkeypatch.setattr(plaza.agent_dao, "get", AsyncMock(return_value=agent))
    create_post = AsyncMock(return_value=post)
    monkeypatch.setattr(plaza.plaza_post_dao, "create_post", create_post)
    list_for_tenant = AsyncMock(return_value=[_agent(mentioned_id, name="Bob", tenant_id=tenant_id)])
    monkeypatch.setattr(plaza.agent_dao, "list_for_tenant", list_for_tenant)
    notifications = []

    async def fake_send_notification(*args, **kwargs):
        notifications.append((args, kwargs))

    monkeypatch.setattr(notification_service, "send_notification", fake_send_notification)

    result = await agent_tools._plaza_create_post(agent_id, {"content": content})

    assert result == f"Post published! (ID: {post_id})"
    assert create_post.await_args is not None
    obj_in = create_post.await_args.args[0] if create_post.await_args.args else create_post.await_args.kwargs["obj_in"]
    assert obj_in["content"] == content[:500]
    list_for_tenant.assert_awaited_once_with(tenant_id)
    assert [notification[1]["agent_id"] for notification in notifications] == [mentioned_id]
    assert notifications[0][1]["type"] == "mention"
    assert notifications[0][1]["body"] == content[:150]


async def test_create_post_swallows_notification_failure_and_commits(monkeypatch, plaza):
    agent_id = uuid.uuid4()
    post_id = uuid.uuid4()
    agent = _agent(agent_id)
    monkeypatch.setattr(plaza.agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr(plaza.plaza_post_dao, "create_post", AsyncMock(return_value=SimpleNamespace(id=post_id)))
    monkeypatch.setattr(
        plaza.agent_dao,
        "list_for_tenant",
        AsyncMock(return_value=[_agent(uuid.uuid4(), name="Bob")]),
    )

    async def failing_notification(*_args, **_kwargs):
        raise RuntimeError("notification unavailable")

    monkeypatch.setattr(notification_service, "send_notification", failing_notification)

    result = await agent_tools._plaza_create_post(agent_id, {"content": "@Bob hello"})

    assert result == f"Post published! (ID: {post_id})"


async def test_create_post_truncates_database_failure(monkeypatch, plaza):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(plaza.agent_dao, "get", AsyncMock(side_effect=RuntimeError("x" * 250)))

    result = await agent_tools._plaza_create_post(agent_id, {"content": "Hello"})

    assert result == f"Failed to create post: {'x' * 200}"


async def test_add_comment_rejects_blank_content_and_invalid_post_id():
    agent_id = uuid.uuid4()

    assert await agent_tools._plaza_add_comment(agent_id, {"post_id": uuid.uuid4(), "content": "  "}) == (
        "Error: Comment content cannot be empty."
    )
    assert await agent_tools._plaza_add_comment(agent_id, {"post_id": "not-a-uuid", "content": "Hello"}) == (
        "Error: Invalid post_id format."
    )


@pytest.mark.parametrize(
    ("post", "agent", "expected"),
    [
        (None, None, "Error: Post not found."),
        (
            SimpleNamespace(id=uuid.uuid4(), author_id=uuid.uuid4(), author_name="Mira"),
            None,
            "Error: Agent not found.",
        ),
        (
            SimpleNamespace(id=uuid.uuid4(), author_id=uuid.uuid4(), author_name="Mira"),
            _agent(uuid.uuid4(), is_system=True),
            "System agents are not allowed to comment on Plaza posts.",
        ),
        (
            SimpleNamespace(id=uuid.uuid4(), author_id=uuid.uuid4(), author_name="Mira"),
            _agent(uuid.uuid4(), access_mode="private"),
            "Only company-wide agents are allowed to comment on Plaza posts.",
        ),
    ],
)
async def test_add_comment_returns_access_guards(monkeypatch, plaza, post, agent, expected):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(plaza.plaza_post_dao, "get_post", AsyncMock(return_value=post))
    monkeypatch.setattr(plaza.agent_dao, "get", AsyncMock(return_value=agent))

    result = await agent_tools._plaza_add_comment(agent_id, {"post_id": uuid.uuid4(), "content": "Hello"})

    assert result == expected


async def test_add_comment_truncates_content_notifies_participants_and_mentions(monkeypatch, plaza):
    agent_id = uuid.uuid4()
    post_id = uuid.uuid4()
    post_author_id = uuid.uuid4()
    other_commenter_id = uuid.uuid4()
    mentioned_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    content = "@Bob " + "x" * 400
    post = SimpleNamespace(
        id=post_id,
        author_id=post_author_id,
        author_type="agent",
        author_name="Mira",
        comments_count=0,
    )
    agent = _agent(agent_id)
    create_comment = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    increment = AsyncMock(return_value=post)
    monkeypatch.setattr(plaza.plaza_post_dao, "get_post", AsyncMock(return_value=post))
    monkeypatch.setattr(
        plaza.agent_dao,
        "get",
        AsyncMock(
            side_effect=[
                agent,
                _agent(post_author_id, name="Mira", creator_id=creator_id),
            ]
        ),
    )
    monkeypatch.setattr(plaza.plaza_comment_dao, "create_comment", create_comment)
    monkeypatch.setattr(plaza.plaza_post_dao, "increment_comments_count", increment)
    monkeypatch.setattr(
        plaza.plaza_comment_dao,
        "list_distinct_comment_authors",
        AsyncMock(
            return_value=[(post_author_id, "agent"), (other_commenter_id, "agent"), (other_commenter_id, "agent")]
        ),
    )
    monkeypatch.setattr(
        plaza.agent_dao,
        "list_for_tenant",
        AsyncMock(return_value=[_agent(mentioned_id, name="Bob")]),
    )
    notifications = []

    async def fake_send_notification(*args, **kwargs):
        notifications.append((args, kwargs))

    monkeypatch.setattr(notification_service, "send_notification", fake_send_notification)

    result = await agent_tools._plaza_add_comment(agent_id, {"post_id": post_id, "content": content})

    assert result == "Comment added to post by Mira."
    obj_in = (
        create_comment.await_args.args[0]
        if create_comment.await_args.args
        else create_comment.await_args.kwargs["obj_in"]
    )
    assert obj_in["content"] == content[:300]
    increment.assert_awaited_once_with(post_id)
    assert [notification[1]["type"] for notification in notifications] == [
        "plaza_reply",
        "plaza_comment",
        "plaza_reply",
        "mention",
    ]
    assert notifications[0][1]["agent_id"] == post_author_id
    assert notifications[1][1]["user_id"] == creator_id
    assert notifications[2][1]["agent_id"] == other_commenter_id
    assert notifications[3][1]["agent_id"] == mentioned_id


async def test_add_comment_notifies_a_human_post_author(monkeypatch, plaza):
    agent_id = uuid.uuid4()
    post_id = uuid.uuid4()
    human_author_id = uuid.uuid4()
    post = SimpleNamespace(
        id=post_id,
        author_id=human_author_id,
        author_type="human",
        author_name="Mira",
        comments_count=None,
    )
    monkeypatch.setattr(plaza.plaza_post_dao, "get_post", AsyncMock(return_value=post))
    monkeypatch.setattr(plaza.agent_dao, "get", AsyncMock(return_value=_agent(agent_id)))
    monkeypatch.setattr(
        plaza.plaza_comment_dao, "create_comment", AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    )
    monkeypatch.setattr(plaza.plaza_post_dao, "increment_comments_count", AsyncMock(return_value=post))
    monkeypatch.setattr(plaza.plaza_comment_dao, "list_distinct_comment_authors", AsyncMock(return_value=[]))
    notifications = []

    async def fake_send_notification(*args, **kwargs):
        notifications.append((args, kwargs))

    monkeypatch.setattr(notification_service, "send_notification", fake_send_notification)

    result = await agent_tools._plaza_add_comment(agent_id, {"post_id": post_id, "content": "Hello"})

    assert result == "Comment added to post by Mira."
    assert notifications[0][1]["user_id"] == human_author_id
    assert notifications[0][1]["type"] == "plaza_reply"


async def test_add_comment_swallows_notification_failure(monkeypatch, plaza):
    agent_id = uuid.uuid4()
    post_id = uuid.uuid4()
    post = SimpleNamespace(
        id=post_id,
        author_id=uuid.uuid4(),
        author_type="human",
        author_name="Mira",
        comments_count=0,
    )
    monkeypatch.setattr(plaza.plaza_post_dao, "get_post", AsyncMock(return_value=post))
    monkeypatch.setattr(plaza.agent_dao, "get", AsyncMock(return_value=_agent(agent_id)))
    monkeypatch.setattr(
        plaza.plaza_comment_dao, "create_comment", AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    )
    monkeypatch.setattr(plaza.plaza_post_dao, "increment_comments_count", AsyncMock(return_value=post))
    monkeypatch.setattr(plaza.plaza_comment_dao, "list_distinct_comment_authors", AsyncMock(return_value=[]))

    async def failing_notification(*_args, **_kwargs):
        raise RuntimeError("notification unavailable")

    monkeypatch.setattr(notification_service, "send_notification", failing_notification)

    result = await agent_tools._plaza_add_comment(agent_id, {"post_id": post_id, "content": "Hello"})

    assert result == "Comment added to post by Mira."


async def test_add_comment_truncates_database_failure(monkeypatch, plaza):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(plaza.plaza_post_dao, "get_post", AsyncMock(side_effect=RuntimeError("x" * 250)))

    result = await agent_tools._plaza_add_comment(agent_id, {"post_id": uuid.uuid4(), "content": "Hello"})

    assert result == f"Failed to add comment: {'x' * 200}"


async def test_legacy_plaza_facade_delegates_to_extracted_module(monkeypatch):
    plaza_mod = importlib.import_module("app.services.agent_tool_exec.plaza")
    agent_id = uuid.uuid4()
    get_arguments = {"limit": 3}
    post_arguments = {"content": "Hello"}
    comment_arguments = {"post_id": str(uuid.uuid4()), "content": "Hello"}
    calls = []

    async def fake_get_new_posts(received_agent_id, received_arguments):
        calls.append(("get", received_agent_id, received_arguments))
        return "get sentinel"

    async def fake_create_post(received_agent_id, received_arguments):
        calls.append(("create", received_agent_id, received_arguments))
        return "create sentinel"

    async def fake_add_comment(received_agent_id, received_arguments):
        calls.append(("comment", received_agent_id, received_arguments))
        return "comment sentinel"

    assert signature(agent_tools._plaza_get_new_posts) == signature(plaza_mod._plaza_get_new_posts)
    assert signature(agent_tools._plaza_create_post) == signature(plaza_mod._plaza_create_post)
    assert signature(agent_tools._plaza_add_comment) == signature(plaza_mod._plaza_add_comment)
    monkeypatch.setattr(plaza_mod, "_plaza_get_new_posts", fake_get_new_posts)
    monkeypatch.setattr(plaza_mod, "_plaza_create_post", fake_create_post)
    monkeypatch.setattr(plaza_mod, "_plaza_add_comment", fake_add_comment)

    assert await agent_tools._plaza_get_new_posts(agent_id, get_arguments) == "get sentinel"
    assert await agent_tools._plaza_create_post(agent_id, post_arguments) == "create sentinel"
    assert await agent_tools._plaza_add_comment(agent_id, comment_arguments) == "comment sentinel"
    assert calls == [
        ("get", agent_id, get_arguments),
        ("create", agent_id, post_arguments),
        ("comment", agent_id, comment_arguments),
    ]
    assert calls[0][2] is get_arguments
    assert calls[1][2] is post_arguments
    assert calls[2][2] is comment_arguments
