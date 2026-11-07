from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI

from app.api import clawbook
from app.core.domain.users import UserRole
from app.dao.clawbook_dao import clawbook_comment_dao, clawbook_post_dao
from app.db.connection import DbConnection
from app.records.user import UserRecord

pytest_plugins = ["test_clawbook_likes_postgres"]


async def test_feed_and_comments_page_ties_hidden_authors_and_tenant_scope(clawbook_db: DbConnection) -> None:
    tenant = uuid4()
    actor = UserRecord(id=uuid4(), tenant_id=tenant, role=UserRole.MEMBER)
    timestamp = datetime(2030, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    await clawbook_db.execute("ALTER TABLE agents DROP CONSTRAINT agents_creator_id_fkey")
    hidden_agents = []
    for mode, system in [("user", False), ("custom", False), ("company", True), ("company", False)]:
        agent = SimpleNamespace(id=uuid4(), name=mode)
        await clawbook_db.execute(
            "INSERT INTO agents (id, name, role_description, creator_id, max_triggers, min_poll_interval_min, "
            "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes, is_system) "
            "VALUES (%(id)s, %(name)s, 'QA', %(creator)s, 1, 1, 1, %(mode)s, 'use', 60, %(system)s)",
            {"id": agent.id, "name": mode, "creator": actor.id, "mode": mode, "system": system},
        )
        hidden_agents.append(agent)
    visible_ids = [UUID(int=value) for value in [10, 20, 30]]
    for post_id in visible_ids:
        await clawbook_post_dao.create_post(
            {
                "id": post_id,
                "author_id": actor.id,
                "author_type": "human",
                "author_name": "Member",
                "content": "Visible",
                "tenant_id": tenant,
                "created_at": timestamp,
            }
        )
    for index, agent in enumerate(hidden_agents):
        await clawbook_post_dao.create_post(
            {
                "author_id": agent.id,
                "author_type": "agent",
                "author_name": agent.name,
                "content": "Agent",
                "tenant_id": tenant if index < 3 else uuid4(),
                "created_at": timestamp,
            }
        )
    await clawbook_post_dao.create_post(
        {
            "id": UUID(int=40),
            "author_id": actor.id,
            "author_type": "human",
            "author_name": "Member",
            "content": "One microsecond newer",
            "tenant_id": tenant,
            "created_at": timestamp + timedelta(microseconds=1),
        }
    )
    for index in range(8):
        await clawbook_comment_dao.create_comment(
            {
                "id": UUID(int=index + 1),
                "post_id": visible_ids[0],
                "author_id": hidden_agents[index].id if index < 3 else actor.id,
                "author_type": "agent" if index < 3 else "human",
                "author_name": "Commenter",
                "content": str(index),
                "created_at": timestamp,
            }
        )
    app = FastAPI()
    app.include_router(clawbook.router)
    app.dependency_overrides[clawbook.get_current_user] = lambda: actor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        ids = []
        cursor = None
        while True:
            response = await client.get(
                "/api/clawbook/posts/page", params={"limit": 2, **({"cursor": cursor} if cursor else {})}
            )
            assert response.status_code == 200
            ids.extend(item["id"] for item in response.json()["items"])
            cursor = response.json()["next_cursor"]
            if cursor is None:
                break
        assert ids == [str(UUID(int=value)) for value in [40, 30, 20, 10]]
        first = await client.get(f"/api/clawbook/posts/{visible_ids[0]}/comments/page", params={"limit": 3})
        assert first.status_code == 200
        assert [item["content"] for item in first.json()["items"]] == ["3", "4", "5"]
        second = await client.get(
            f"/api/clawbook/posts/{visible_ids[0]}/comments/page",
            params={"limit": 3, "cursor": first.json()["next_cursor"]},
        )
        assert [item["content"] for item in second.json()["items"]] == ["6", "7"]
        assert second.json()["next_cursor"] is None
        actor.role = UserRole.PLATFORM_ADMIN
        actor.tenant_id = None
        all_tenants = await client.get("/api/clawbook/posts/page")
        assert len(all_tenants.json()["items"]) == 5
    app.dependency_overrides.clear()
    tool_comments = await clawbook_comment_dao.list_comments_for_post(visible_ids[0])
    assert [comment.content for comment in tool_comments] == ["0", "1", "2", "3", "4"]
