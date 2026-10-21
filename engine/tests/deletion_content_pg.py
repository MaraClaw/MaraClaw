from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from app.db.session import connection_ctx

if TYPE_CHECKING:
    from tenant_deletion_pg_fixtures import DeletionPostgres


@dataclass(frozen=True, slots=True)
class DeletionContent:
    peer_id: UUID
    agent_post_id: UUID
    surviving_post_id: UUID
    untouched_post_id: UUID


async def seed_deletion_content(fixture: DeletionPostgres) -> DeletionContent:
    peer_id, agent_post_id, user_post_id, tenant_post_id, surviving_post_id, untouched_post_id = (
        uuid4() for _ in range(6)
    )
    participants = {ref: uuid4() for ref in (fixture.agent_id, fixture.actor.id, peer_id, fixture.other_user_id)}
    async with connection_ctx() as db:
        await db.execute(
            "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, agent_type, "
            "max_triggers, min_poll_interval_min, webhook_rate_limit, access_mode, company_access_level, "
            "max_llm_calls_per_day, heartbeat_interval_minutes) "
            "VALUES (%s, 'Peer', 'Role', %s, %s, 'employee', 1, 5, 5, 'private', 'use', 100, 240)",
            (peer_id, fixture.other_user_id, fixture.other_tenant_id),
        )
        for ref_id, participant_id in participants.items():
            await db.execute(
                "INSERT INTO participants (id, type, ref_id, display_name) VALUES (%s, %s, %s, 'Participant')",
                (participant_id, "agent" if ref_id in (fixture.agent_id, peer_id) else "user", ref_id),
            )
        for post_id, author_id, author_type, tenant_id in (
            (agent_post_id, fixture.agent_id, "agent", None),
            (user_post_id, fixture.actor.id, "user", fixture.other_tenant_id),
            (tenant_post_id, fixture.other_user_id, "user", fixture.tenant_id),
            (surviving_post_id, peer_id, "agent", fixture.other_tenant_id),
            (untouched_post_id, fixture.other_user_id, "user", fixture.other_tenant_id),
        ):
            await db.execute(
                "INSERT INTO plaza_posts (id, author_id, author_type, author_name, content, tenant_id, "
                "likes_count, comments_count) VALUES (%s, %s, %s, 'Author', 'Post', %s, 0, 0)",
                (post_id, author_id, author_type, tenant_id),
            )
            if post_id == untouched_post_id:
                continue
            for author, kind in (
                (fixture.agent_id, "agent"),
                (fixture.actor.id, "user"),
                (fixture.other_user_id, "user"),
            ):
                await db.execute(
                    "INSERT INTO plaza_comments (id, post_id, author_id, author_type, author_name, content) "
                    "VALUES (%s, %s, %s, %s, 'Author', 'Comment')",
                    (uuid4(), post_id, author, kind),
                )
                await db.execute(
                    "INSERT INTO plaza_likes (id, post_id, author_id, author_type) VALUES (%s, %s, %s, %s)",
                    (uuid4(), post_id, author, kind),
                )
            await db.execute("UPDATE plaza_posts SET likes_count = 3, comments_count = 3 WHERE id = %s", (post_id,))
        for ref_id in (fixture.agent_id, fixture.actor.id, fixture.other_user_id):
            await db.execute(
                "INSERT INTO chat_sessions (id, agent_id, user_id, title, participant_id, peer_agent_id) "
                "VALUES (%s, %s, %s, 'Peer conversation', %s, %s)",
                (
                    uuid4(),
                    peer_id,
                    fixture.other_user_id,
                    participants[ref_id],
                    fixture.agent_id if ref_id == fixture.agent_id else None,
                ),
            )
            await db.execute(
                "INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id, participant_id) "
                "VALUES (%s, %s, %s, 'assistant', 'Peer message', %s, %s)",
                (uuid4(), peer_id, fixture.other_user_id, str(uuid4()), participants[ref_id]),
            )
        for tenant_id, user_id in (
            (fixture.tenant_id, None),
            (None, fixture.actor.id),
            (fixture.other_tenant_id, fixture.other_user_id),
        ):
            await db.execute(
                "INSERT INTO sso_scan_sessions (id, status, tenant_id, user_id, access_token, expires_at) "
                "VALUES (%s, 'completed', %s, %s, 'token', now() + interval '1 hour')",
                (uuid4(), tenant_id, user_id),
            )
        task_id = uuid4()
        await db.execute(
            "INSERT INTO tasks (id, agent_id, title, type, status, priority, assignee, created_by) "
            "VALUES (%s, %s, 'History survives', 'todo', 'done', 'high', 'self', %s)",
            (task_id, fixture.agent_id, fixture.actor.id),
        )
        await db.execute(
            "INSERT INTO task_logs (id, task_id, content) VALUES (%s, %s, 'Completed')", (uuid4(), task_id)
        )
        await db.execute(
            "INSERT INTO web_search_events (id, agent_id, tenant_id, kind, method, http_status, status_class, "
            "latency_ms, query_hash) VALUES (%s, %s, %s, 'search', 'POST', 200, 'success', 1, 'hash')",
            (uuid4(), fixture.agent_id, fixture.tenant_id),
        )
    workspace = fixture.runtime_root / str(fixture.agent_id)
    workspace.mkdir()
    (workspace / "SOUL.md").write_text("workspace retained until commit")
    return DeletionContent(peer_id, agent_post_id, surviving_post_id, untouched_post_id)
