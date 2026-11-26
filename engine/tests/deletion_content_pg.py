from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from app.core.json_types import uuid_from_row
from app.db.session import connection_ctx

if TYPE_CHECKING:
    from tenant_deletion_pg_fixtures import DeletionPostgres


@dataclass(frozen=True, slots=True)
class DeletionBranches:
    """Ids by table: rows only the agent's deletion removes, rows only the company's deletion adds, and survivors."""

    agent: dict[str, list[UUID]]
    tenant: dict[str, list[UUID]]
    kept: dict[str, list[UUID]]


@dataclass(frozen=True, slots=True)
class DeletionContent:
    peer_id: UUID
    agent_post_id: UUID
    surviving_post_id: UUID
    untouched_post_id: UUID


async def surviving(fixture: DeletionPostgres, ids: dict[str, list[UUID]]) -> dict[str, set[UUID]]:
    """For each table, which of the given ids are still stored."""
    found: dict[str, set[UUID]] = {}
    for table, table_ids in ids.items():
        rows = await fixture.rows(f"SELECT id FROM {table} WHERE id = ANY(%s)", (table_ids,))  # noqa: S608
        found[table] = {uuid_from_row(row["id"]) for row in rows}
    return found


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
            "VALUES (%s, 'Peer', 'Role', %s, %s, 'openclaw', 1, 5, 5, 'private', 'use', 100, 240)",
            (peer_id, fixture.other_user_id, fixture.other_tenant_id),
        )
        for ref_id, participant_id in participants.items():
            await db.execute(
                "INSERT INTO participants (id, type, ref_id, display_name) VALUES (%s, %s, %s, 'Participant')",
                (participant_id, "agent" if ref_id in (fixture.agent_id, peer_id) else "user", ref_id),
            )
        for post_id, author_id, author_type, tenant_id in (
            (agent_post_id, fixture.agent_id, "agent", None),
            (user_post_id, fixture.actor.id, "human", fixture.other_tenant_id),
            (tenant_post_id, fixture.other_user_id, "human", fixture.tenant_id),
            (surviving_post_id, peer_id, "agent", fixture.other_tenant_id),
            (untouched_post_id, fixture.other_user_id, "human", fixture.other_tenant_id),
        ):
            await db.execute(
                "INSERT INTO clawbook_posts (id, author_id, author_type, author_name, content, tenant_id, "
                "likes_count, comments_count) VALUES (%s, %s, %s, 'Author', 'Post', %s, 0, 0)",
                (post_id, author_id, author_type, tenant_id),
            )
            if post_id == untouched_post_id:
                continue
            for author, kind in (
                (fixture.agent_id, "agent"),
                (fixture.actor.id, "human"),
                (fixture.other_user_id, "human"),
            ):
                await db.execute(
                    "INSERT INTO clawbook_comments (id, post_id, author_id, author_type, author_name, content) "
                    "VALUES (%s, %s, %s, %s, 'Author', 'Comment')",
                    (uuid4(), post_id, author, kind),
                )
                await db.execute(
                    "INSERT INTO clawbook_likes (id, post_id, author_id, author_type) VALUES (%s, %s, %s, %s)",
                    (uuid4(), post_id, author, kind),
                )
            await db.execute("UPDATE clawbook_posts SET likes_count = 3, comments_count = 3 WHERE id = %s", (post_id,))
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


async def seed_deletion_branches(fixture: DeletionPostgres) -> DeletionBranches:
    """One row per deletion predicate branch, each matching that branch alone, beside rows no branch may touch.

    The fixture's agent and actor belong to the target company; the peer agent and the other user to the other one.
    """
    peer_id = uuid4()
    agent_part, user_part, peer_part, other_part = (uuid4() for _ in range(4))
    tables = ("chat_messages", "chat_sessions", "gateway_messages", "audit_logs", "approval_requests", "participants")
    agent, tenant, kept = ({table: [] for table in tables} for _ in range(3))

    def row(bucket: dict[str, list[UUID]], table: str) -> UUID:
        bucket[table].append(row_id := uuid4())
        return row_id

    async with connection_ctx() as db:
        await db.execute(
            "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, agent_type, "
            "max_triggers, min_poll_interval_min, webhook_rate_limit, access_mode, company_access_level, "
            "max_llm_calls_per_day, heartbeat_interval_minutes) "
            "VALUES (%s, 'Peer', 'Role', %s, %s, 'openclaw', 1, 5, 5, 'private', 'use', 100, 240)",
            (peer_id, fixture.other_user_id, fixture.other_tenant_id),
        )
        for bucket, participant_id, kind, ref_id in (
            (agent, agent_part, "agent", fixture.agent_id),
            (tenant, user_part, "user", fixture.actor.id),
            (kept, peer_part, "agent", peer_id),
            (kept, other_part, "user", fixture.other_user_id),
        ):
            bucket["participants"].append(participant_id)
            await db.execute(
                "INSERT INTO participants (id, type, ref_id, display_name) VALUES (%s, %s, %s, 'Participant')",
                (participant_id, kind, ref_id),
            )
        # Sessions, by bucket then agent, user, participant and peer agent.
        for bucket, agent_id, user_id, participant_id, peer_agent_id in (
            (agent, fixture.agent_id, fixture.other_user_id, None, None),
            (agent, peer_id, fixture.other_user_id, None, fixture.agent_id),
            (agent, peer_id, fixture.other_user_id, agent_part, None),
            (tenant, peer_id, fixture.actor.id, None, None),
            (tenant, peer_id, fixture.other_user_id, user_part, None),
            (kept, peer_id, fixture.other_user_id, peer_part, None),
            (kept, peer_id, fixture.other_user_id, other_part, peer_id),
        ):
            await db.execute(
                "INSERT INTO chat_sessions (id, agent_id, user_id, title, participant_id, peer_agent_id) "
                "VALUES (%s, %s, %s, 'Branch', %s, %s)",
                (row(bucket, "chat_sessions"), agent_id, user_id, participant_id, peer_agent_id),
            )
        # Messages, by bucket then agent, user and participant; a feishu_ conversation id is not a UUID.
        for bucket, agent_id, user_id, participant_id in (
            (agent, fixture.agent_id, fixture.other_user_id, None),
            (agent, peer_id, fixture.other_user_id, agent_part),
            (tenant, peer_id, fixture.actor.id, None),
            (tenant, peer_id, fixture.other_user_id, user_part),
            (kept, peer_id, fixture.other_user_id, peer_part),
            (kept, peer_id, fixture.other_user_id, None),
        ):
            await db.execute(
                "INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id, participant_id) "
                "VALUES (%s, %s, %s, 'user', 'Branch', %s, %s)",
                (row(bucket, "chat_messages"), agent_id, user_id, f"feishu_{uuid4().hex}", participant_id),
            )
        # Gateway messages, by bucket then agent, sender agent and sender user.
        for bucket, agent_id, sender_agent_id, sender_user_id in (
            (agent, fixture.agent_id, None, None),
            (agent, peer_id, fixture.agent_id, None),
            (tenant, peer_id, None, fixture.actor.id),
            (kept, peer_id, peer_id, fixture.other_user_id),
        ):
            await db.execute(
                "INSERT INTO gateway_messages (id, agent_id, sender_agent_id, sender_user_id, content, status) "
                "VALUES (%s, %s, %s, %s, 'Branch', 'completed')",
                (row(bucket, "gateway_messages"), agent_id, sender_agent_id, sender_user_id),
            )
        # Audit logs, by bucket then agent and user.
        for bucket, agent_id, user_id in (
            (agent, fixture.agent_id, None),
            (tenant, None, fixture.actor.id),
            (kept, peer_id, fixture.other_user_id),
        ):
            await db.execute(
                "INSERT INTO audit_logs (id, user_id, agent_id, action, details) VALUES (%s, %s, %s, 'branch', '{}')",
                (row(bucket, "audit_logs"), user_id, agent_id),
            )
        # Approvals, by bucket then agent and resolver.
        for bucket, agent_id, resolved_by in (
            (agent, fixture.agent_id, None),
            (tenant, peer_id, fixture.actor.id),
            (kept, peer_id, fixture.other_user_id),
        ):
            await db.execute(
                "INSERT INTO approval_requests (id, agent_id, action_type, details, status, resolved_by) "
                "VALUES (%s, %s, 'branch', '{}', 'approved', %s)",
                (row(bucket, "approval_requests"), agent_id, resolved_by),
            )
    return DeletionBranches(agent, tenant, kept)
