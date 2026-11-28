"""Exercise the production keyset queries on the plan psycopg can reuse across pages."""

from collections.abc import Awaitable, Callable, Sequence
from datetime import date, timedelta
from uuid import MAX, UUID

import pytest

import test_shared_gateway_postgres as shared_fixtures
from app.dao.agent_dao import agent_dao
from app.dao.capacity_order_dao import capacity_order_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.openclaw_reply_dao import openclaw_reply_dao
from app.dao.openclaw_update_dao import openclaw_update_run_dao
from app.db.session import transaction
from app.records.agent import AgentRecord
from test_chat_submission_postgres import ReceiptDatabase
from test_deletion_index_plans_postgres import nodes
from test_gateway_lane_reads_postgres import AT, dao_statements, generic_plan, outline, seed_lane

receipt_database = shared_fixtures.receipt_database
pooled_database = shared_fixtures.pooled_database

_AFTER_ID = UUID(int=1800)


async def _assert_seek[T](db: ReceiptDatabase, call: Callable[[], Awaitable[T]], index: str, bound: str) -> T:
    page, statements = await dao_statements(call)
    (statement,) = statements
    plan = await generic_plan(db, statement)
    assert any(node.get("Index Name") == index and bound in str(node.get("Index Cond", "")) for node in nodes(plan)), (
        outline(plan)
    )
    return page


@pytest.fixture
async def roster(pooled_database: ReceiptDatabase) -> ReceiptDatabase:
    db = pooled_database
    tenant_id = UUID(int=1)
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO tenants (id, name, slug, tier) VALUES (%(id)s, 'Company', 'keyset-roster', 'pro')",
            {"id": tenant_id},
        )
        await db.first.execute(
            "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, status, max_triggers, "
            "min_poll_interval_min, webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
            "SELECT lpad(to_hex(n), 32, '0')::uuid, 'Agent', 'Role', %(user)s, %(tenant)s, 'idle', 1, 5, 5, "
            "'private', 'use', 240 FROM generate_series(0, 2400) n",
            {"user": db.user.id, "tenant": tenant_id},
        )
        await db.first.execute(
            "INSERT INTO agent_workspace_usage (agent_id, dir_bytes, layer_bytes, limit_bytes, remeasure_requested_at) "
            "SELECT id, 0, 0, 1, now() FROM agents"
        )
        await db.first.execute("ANALYZE agents, agent_workspace_usage")
    return db


async def test_skill_sweep_pages_include_the_lowest_uuid_and_seek_by_primary_key(roster: ReceiptDatabase) -> None:
    first = await _assert_seek(roster, lambda: agent_dao.list_ids_page(after_id=None, limit=2), "agents_pkey", "id >=")
    page = await _assert_seek(
        roster, lambda: agent_dao.list_ids_page(after_id=_AFTER_ID, limit=2), "agents_pkey", "id >="
    )
    assert first == [UUID(int=0), UUID(int=1)]
    assert page == [UUID(int=1801), UUID(int=1802)]


@pytest.mark.parametrize(
    "method",
    [
        agent_dao.list_workspace_quota_page,
        agent_dao.list_workspace_quota_priority_page,
        agent_dao.list_workspace_mirror_page,
    ],
    ids=["quota", "quota-priority", "mirror"],
)
async def test_workspace_pages_keep_the_uuid_seek_in_a_generic_plan(
    roster: ReceiptDatabase,
    method: Callable[[UUID | None, int], Awaitable[Sequence[AgentRecord]]],
) -> None:
    first = await _assert_seek(roster, lambda: method(None, 2), "agents_pkey", "id >=")
    page = await _assert_seek(roster, lambda: method(_AFTER_ID, 2), "agents_pkey", "id >=")
    assert [record.id for record in first] == [UUID(int=0), UUID(int=1)]
    assert [record.id for record in page] == [UUID(int=1801), UUID(int=1802)]


async def test_reply_binding_page_seeks_to_the_next_agent_group(roster: ReceiptDatabase) -> None:
    async with transaction(roster.first):
        await roster.first.execute(
            "WITH seeded AS (INSERT INTO chat_sessions (id, agent_id, user_id, title) "
            "SELECT gen_random_uuid(), id, creator_id, 'Reply session' FROM agents RETURNING id, agent_id, user_id) "
            "INSERT INTO openclaw_reply_bindings (id, session_id, agent_id, tenant_id, owner_id, user_id) "
            "SELECT gen_random_uuid(), s.id, s.agent_id, a.tenant_id, a.creator_id, s.user_id "
            "FROM seeded s JOIN agents a ON a.id = s.agent_id"
        )
        await roster.first.execute("ANALYZE openclaw_reply_bindings")
    for after, first in ((None, 0), (_AFTER_ID, 1801)):
        page = await _assert_seek(
            roster,
            lambda after=after: openclaw_reply_dao.page(after),
            "ix_openclaw_reply_bindings_agent_id_id",
            "agent_id >=",
        )
        assert page[0].agent_id == UUID(int=first)


async def test_recent_successes_page_seeks_by_id(roster: ReceiptDatabase) -> None:
    async with transaction(roster.first):
        await roster.first.execute(
            "WITH notice AS (INSERT INTO openclaw_update_notices (id, request_id, title, body, mandatory) "
            "VALUES (gen_random_uuid(), gen_random_uuid(), 'Update', 'Notes', false) RETURNING id) "
            "INSERT INTO openclaw_update_runs (id, notice_id, agent_id, state, updated_at) "
            "SELECT a.id, n.id, a.id, 'succeeded', %(now)s FROM agents a CROSS JOIN notice n",
            {"now": AT},
        )
        await roster.first.execute("ANALYZE openclaw_update_runs")
    for after, first in ((None, 0), (_AFTER_ID, 1801)):
        page = await _assert_seek(
            roster,
            lambda after=after: openclaw_update_run_dao.list_recent_succeeded(AT, after),
            "openclaw_update_runs_pkey",
            "id >=",
        )
        assert page[0].id == UUID(int=first)


async def test_tenants_due_for_renewal_seek_inside_the_approved_month(pooled_database: ReceiptDatabase) -> None:
    db = pooled_database
    async with transaction(db.first):
        await db.first.execute(
            "WITH tenants_seeded AS (INSERT INTO tenants (id, name, slug, tier) "
            "SELECT lpad(to_hex(n), 32, '0')::uuid, 'Company', 'keyset-' || n, 'pro' "
            "FROM generate_series(1, 2400) n RETURNING id, slug) "
            "INSERT INTO capacity_orders (id, tenant_id, reference, kind, status, tier, billing_month, extra_accounts, "
            "extra_agents, account_unit_usd, agent_unit_usd, days_charged, days_in_month, amount_usd_cents, usd_vnd_rate, "
            "amount_vnd, recipient, evidence_storage_key, evidence_content_type, evidence_filename, evidence_size_bytes, "
            "evidence_sha256, submitted_at, reviewed_at) "
            "SELECT gen_random_uuid(), id, 'MC' || translate(lpad(substr(slug, 8), 8, '0'), '0123456789', 'ABCDEFGHIJ'), "
            "'purchase', 'approved', 'pro', '2026-10-01', 1, 0, 5, 35, 31, 31, 500, 25000, 125000, '{}'::jsonb, "
            "slug, 'image/png', 'proof.png', 10, repeat('ab', 32), now(), now() FROM tenants_seeded"
        )
        await db.first.execute("ANALYZE capacity_orders")
    for after, first in ((None, 1), (_AFTER_ID, 1801)):
        page = await _assert_seek(
            db,
            lambda after=after: capacity_order_dao.tenants_due_for_renewal(
                date(2026, 11, 1), date(2026, 10, 1), after=after, limit=2
            ),
            "ix_capacity_orders_month_approved",
            "tenant_id >=",
        )
        assert page == [UUID(int=first), UUID(int=first + 1)]


async def test_lane_heads_seek_to_the_composite_bound(pooled_database: ReceiptDatabase) -> None:
    db = pooled_database
    await seed_lane(db, "", 1, 2400)
    for after, first in ((None, 1), ((AT + timedelta(seconds=1800), MAX), 1801)):
        page, _ = await _assert_seek(
            db,
            lambda after=after: gateway_message_dao.lane_heads_page(db.agent.id, 2, after=after),
            "ix_gateway_messages_pending_order",
            "ROW(created_at, id) >",
        )
        assert [message.created_at for message in page] == [
            AT + timedelta(seconds=first),
            AT + timedelta(seconds=first + 1),
        ]
