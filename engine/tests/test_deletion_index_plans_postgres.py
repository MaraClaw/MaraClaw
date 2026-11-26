"""EXPLAIN plans of agent and company deletion, and of the reply-binding trigger's lookups, over a seeded dataset."""

from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from psycopg import sql

import test_chat_submission_postgres as receipt_fixtures
from app.core.json_types import JsonObject, is_json_object
from app.dao.agent_deletion_sql import AGENT_DELETE_CLEANUP_SQL
from app.dao.tenant_deletion_sql import TENANT_DELETE_CLEANUP_SQL
from app.db.connection import DbConnection, Params
from app.db.session import transaction
from test_chat_submission_postgres import ReceiptDatabase

receipt_database = receipt_fixtures.receipt_db

type Plan = JsonObject

# The tables whose deletes scan history; a sequential scan of one of them is what the indexes exist to avoid.
_HISTORY_TABLES = ("chat_messages", "chat_sessions", "gateway_messages", "audit_logs", "approval_requests")
_BULK_AGENTS = 200
_ROWS_PER_AGENT = 50


@dataclass(frozen=True, slots=True)
class Company:
    tenant_id: UUID
    agent_id: UUID
    session_id: UUID
    binding_id: UUID


async def _seed(db: DbConnection) -> Company:
    """A small target company beside a bulk one: each bulk agent has its own creator, participant and history."""
    tenant_id, bulk_id, user_id, agent_id, session_id, binding_id = (uuid4() for _ in range(6))
    for tid, slug in ((tenant_id, "target"), (bulk_id, "bulk")):
        await db.execute(
            "INSERT INTO tenants (id, name, slug, tier) VALUES (%(id)s, %(slug)s, %(slug)s, 'ultra')",
            {"id": tid, "slug": slug},
        )
    await db.execute(
        "INSERT INTO users (id, display_name, tenant_id) VALUES (%(id)s, 'Target', %(tenant)s)",
        {"id": user_id, "tenant": tenant_id},
    )
    await db.execute(
        "INSERT INTO users (id, display_name, tenant_id) "
        "SELECT gen_random_uuid(), 'Bulk', %(tenant)s FROM generate_series(1, %(count)s)",
        {"tenant": bulk_id, "count": _BULK_AGENTS},
    )
    await db.execute(
        "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, agent_type, max_triggers, "
        "min_poll_interval_min, webhook_rate_limit, access_mode, company_access_level, max_llm_calls_per_day, "
        "heartbeat_interval_minutes) VALUES (%(id)s, 'Agent', 'Role', %(creator)s, %(tenant)s, 'openclaw', 1, 5, 5, "
        "'private', 'use', 100, 240)",
        {"id": agent_id, "creator": user_id, "tenant": tenant_id},
    )
    await db.execute(
        "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, agent_type, max_triggers, "
        "min_poll_interval_min, webhook_rate_limit, access_mode, company_access_level, max_llm_calls_per_day, "
        "heartbeat_interval_minutes) SELECT gen_random_uuid(), 'Agent', 'Role', u.id, u.tenant_id, 'openclaw', 1, 5, "
        "5, 'private', 'use', 100, 240 FROM users u WHERE u.tenant_id = %(tenant)s",
        {"tenant": bulk_id},
    )
    await db.execute(
        "INSERT INTO participants (id, type, ref_id, display_name) "
        "SELECT gen_random_uuid(), 'agent', id, 'Participant' FROM agents"
    )
    history = "FROM agents a CROSS JOIN generate_series(1, %(rows)s) JOIN participants p ON p.ref_id = a.id"
    rows = {"rows": _ROWS_PER_AGENT}
    await db.execute(
        "INSERT INTO chat_sessions (id, agent_id, user_id, title, participant_id) "
        f"SELECT gen_random_uuid(), a.id, a.creator_id, 'Session', p.id {history}",
        rows,
    )
    await db.execute(
        "INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id, participant_id) "
        f"SELECT gen_random_uuid(), a.id, a.creator_id, 'user', 'Message', gen_random_uuid()::text, p.id {history}",
        rows,
    )
    await db.execute(
        "INSERT INTO gateway_messages (id, agent_id, sender_agent_id, sender_user_id, content, status) "
        f"SELECT gen_random_uuid(), a.id, a.id, a.creator_id, 'Message', 'completed' {history}",
        rows,
    )
    await db.execute(
        "INSERT INTO audit_logs (id, user_id, agent_id, action, details) "
        f"SELECT gen_random_uuid(), a.creator_id, a.id, 'action', '{{}}' {history}",
        rows,
    )
    await db.execute(
        "INSERT INTO approval_requests (id, agent_id, action_type, details, status, resolved_by) "
        f"SELECT gen_random_uuid(), a.id, 'action', '{{}}', 'approved', a.creator_id {history}",
        rows,
    )
    await db.execute(
        "INSERT INTO chat_sessions (id, agent_id, user_id, title) VALUES (%(id)s, %(agent)s, %(user)s, 'Bound')",
        {"id": session_id, "agent": agent_id, "user": user_id},
    )
    await db.execute(
        "INSERT INTO openclaw_reply_bindings (id, agent_id, session_id, owner_id, tenant_id, user_id) "
        "VALUES (%(id)s, %(agent)s, %(session)s, %(user)s, %(tenant)s, %(user)s)",
        {"id": binding_id, "agent": agent_id, "session": session_id, "user": user_id, "tenant": tenant_id},
    )
    await db.execute(
        "INSERT INTO openclaw_reply_bindings (id, agent_id, session_id, owner_id, tenant_id, user_id) "
        "SELECT gen_random_uuid(), s.agent_id, s.id, s.user_id, %(tenant)s, s.user_id "
        "FROM chat_sessions s WHERE s.id <> %(session)s LIMIT 2000",
        {"tenant": bulk_id, "session": session_id},
    )
    await db.execute("ANALYZE")
    return Company(tenant_id, agent_id, session_id, binding_id)


async def seeded(db: ReceiptDatabase) -> Company:
    async with transaction(db.first):
        return await _seed(db.first)


def _root(rows: list[dict[str, object]]) -> Plan:
    explained = next(iter(rows[0].values()))
    assert isinstance(explained, list)
    plan: object = explained[0]["Plan"]
    assert is_json_object(plan)
    return plan


async def plan_of(db: ReceiptDatabase, statement: str, params: Params = None) -> Plan:
    """EXPLAIN one statement without running it, as the planner settles on it for these parameter values."""
    async with transaction(db.first):
        return _root(await db.first.fetchall("EXPLAIN (FORMAT JSON) " + statement, params))


async def generic_plan_of(db: ReceiptDatabase, statement: str, argument: UUID) -> Plan:
    """EXPLAIN a one-parameter statement as the generic plan a PL/pgSQL or prepared statement caches."""
    async with transaction(db.first):
        await db.first.execute("SET LOCAL plan_cache_mode = force_generic_plan")
        await db.first.execute(f"PREPARE lookup (uuid) AS {statement}")
        rows = await db.first.fetchall(f"EXPLAIN (FORMAT JSON) EXECUTE lookup ({sql.Literal(argument).as_string()})")
        await db.first.execute("DEALLOCATE lookup")
    return _root(rows)


def nodes(plan: Plan) -> Iterator[Plan]:
    yield plan
    children = plan.get("Plans", [])
    assert isinstance(children, list)
    for child in children:
        assert is_json_object(child)
        yield from nodes(child)


def outline(plan: Plan) -> str:
    return "\n".join(
        ", ".join(f"{key}: {node[key]}" for key in ("Node Type", "Relation Name", "Index Name") if key in node)
        for node in nodes(plan)
    )


def scans(plan: Plan, table: str) -> list[Plan]:
    return [node for node in nodes(plan) if node.get("Relation Name") == table]


def history_deletes(statements: tuple[str, ...]) -> list[tuple[str, str]]:
    """The (table, statement) pairs that delete history; the resolved_by branch has no index and stays out."""
    pairs: list[tuple[str, str]] = []
    for statement in statements:
        table = statement.split()[2] if statement.startswith("DELETE FROM ") else ""
        if table in _HISTORY_TABLES and "resolved_by" not in statement:
            pairs.append((table, statement))
    return pairs


@pytest.mark.parametrize(
    ("key", "statements"),
    [("aid", AGENT_DELETE_CLEANUP_SQL), ("tid", TENANT_DELETE_CLEANUP_SQL)],
    ids=["agent", "company"],
)
async def test_history_deletes_read_their_tables_through_indexes(
    receipt_database: ReceiptDatabase, key: str, statements: tuple[str, ...]
) -> None:
    # Given an agent's and a company's history beside a bulk company with 10000 rows per history table.
    company = await seeded(receipt_database)
    params = {"aid": company.agent_id, "tid": company.tenant_id}
    deletes = history_deletes(statements)
    assert {table for table, _ in deletes} == set(_HISTORY_TABLES)
    for table, statement in deletes:
        # When the planner plans each delete.
        plan = await plan_of(receipt_database, statement, {key: params[key]})
        # Then it reaches the table by index, with no sequential scan of it.
        kinds = [node["Node Type"] for node in scans(plan, table)]
        assert kinds, outline(plan)
        assert "Seq Scan" not in kinds, f"{statement}\n{outline(plan)}"


async def test_reply_binding_lookups_use_their_indexes(receipt_database: ReceiptDatabase) -> None:
    # Given the trigger's two lookups and the foreign key's set-null scan, run standalone with typed parameters.
    company = await seeded(receipt_database)
    lock = "SELECT id FROM chat_sessions WHERE id = $1 FOR UPDATE"
    revoke = "DELETE FROM openclaw_reply_bindings WHERE session_id = $1"
    unbind = "UPDATE gateway_messages SET reply_binding_id = NULL WHERE reply_binding_id = $1"
    for statement, table, index in (
        (lock, "chat_sessions", "chat_sessions_pkey"),
        (revoke, "openclaw_reply_bindings", "openclaw_reply_bindings_session_id_key"),
        (unbind, "gateway_messages", "ix_gateway_messages_reply_binding_id"),
    ):
        # When the generic plan is built, as a cached PL/pgSQL statement builds it.
        plan = await generic_plan_of(receipt_database, statement, company.session_id)
        # Then each reads its table through that one index.
        assert [node.get("Index Name") for node in scans(plan, table) if "Index Name" in node] == [index], outline(plan)
        assert "Seq Scan" not in [node["Node Type"] for node in scans(plan, table)], outline(plan)


async def test_text_cast_lookup_scans_every_session(receipt_database: ReceiptDatabase) -> None:
    # Given the lookup the trigger used to run, comparing the key as text.
    company = await seeded(receipt_database)
    # When the planner plans it.
    plan = await plan_of(
        receipt_database,
        "SELECT id FROM chat_sessions WHERE id::text = %(conversation)s FOR UPDATE",
        {"conversation": str(company.session_id)},
    )
    # Then it reads the whole table, so the checks above can tell an index scan from a table scan.
    assert "Seq Scan" in [node["Node Type"] for node in scans(plan, "chat_sessions")], outline(plan)
