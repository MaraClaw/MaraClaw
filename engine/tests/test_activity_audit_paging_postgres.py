"""First and deep activity/audit pages use the baseline indexes with no sort."""

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Literal, Protocol, assert_never
from uuid import UUID

import pytest

import test_shared_gateway_postgres as shared_fixtures
from app.dao.activity_log_dao import ActivityTimelineQuery, agent_activity_log_dao
from app.dao.audit_log_dao import audit_log_dao
from app.db.session import transaction
from test_chat_submission_postgres import ReceiptDatabase
from test_deletion_index_plans_postgres import nodes, plan_of
from test_gateway_lane_reads_postgres import AT, dao_statements, outline

receipt_database = shared_fixtures.receipt_database
pooled_database = shared_fixtures.pooled_database

TENANT = UUID(int=1)
SCHEDULE = UUID(int=2)
FOREIGN_AGENT = UUID(int=3)
_FIRST = 4000
_DEEP = 1000
_PAGE_SIZE = 20


@pytest.fixture
async def history(pooled_database: ReceiptDatabase) -> ReceiptDatabase:
    db = pooled_database
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO tenants (id, name, slug, tier) VALUES (%(tenant)s, 'Own', 'own', 'pro')",
            {"tenant": TENANT},
        )
        await db.first.execute(
            "UPDATE agents SET tenant_id = %(tenant)s WHERE id = %(agent)s",
            {"tenant": TENANT, "agent": db.agent.id},
        )
        await db.first.execute(
            "INSERT INTO agents (id, name, creator_id, role_description, max_triggers, min_poll_interval_min, "
            "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
            "SELECT %(foreign)s, 'Other Agent', creator_id, role_description, max_triggers, min_poll_interval_min, "
            "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes "
            "FROM agents WHERE id = %(own)s",
            {"foreign": FOREIGN_AGENT, "own": db.agent.id},
        )
        # Half the activity belongs to someone else. Of the agent's own rows, half match this particular schedule.
        await db.first.execute(
            "INSERT INTO agent_activity_logs (id, agent_id, action_type, summary, detail_json, created_at) "
            "SELECT lpad(to_hex(n), 32, '0')::uuid, CASE WHEN n %% 2 = 0 THEN %(own)s ELSE %(foreign)s END, "
            "(CASE WHEN n %% 4 = 0 THEN 'schedule_run' ELSE 'tool_call' END)::activity_action_enum, 'Activity', "
            "json_build_object('schedule_id', %(schedule)s::text), %(at)s + (n / 10) * interval '1 second' "
            "FROM generate_series(1, 4000) n",
            {"own": db.agent.id, "foreign": FOREIGN_AGENT, "schedule": SCHEDULE, "at": AT},
        )
        await db.first.execute(
            "INSERT INTO audit_logs (id, agent_id, action, details, created_at) "
            "SELECT id, agent_id, 'tool_call', '{}', created_at FROM agent_activity_logs"
        )
        await db.first.execute("ANALYZE agents, agent_activity_logs, audit_logs")
    return db


class PageRow(Protocol):
    @property
    def id(self) -> UUID: ...


type Position = tuple[datetime, UUID] | None
type Listing = Callable[[Position], Awaitable[Sequence[PageRow]]]
type View = Literal["activity", "timeline", "schedule", "agent-audit", "tenant-audit", "platform-audit"]


def _listing(db: ReceiptDatabase, view: View) -> tuple[Listing, str, int]:
    match view:
        case "activity":
            return (
                lambda before: agent_activity_log_dao.list_for_agent(db.agent.id, limit=_PAGE_SIZE, before=before),
                "ix_agent_activity_logs_timeline",
                2,
            )
        case "timeline":
            return (
                lambda before: agent_activity_log_dao.list_timeline_page(
                    ActivityTimelineQuery(agent_id=db.agent.id, limit=_PAGE_SIZE, before=before)
                ),
                "ix_agent_activity_logs_timeline",
                2,
            )
        case "schedule":
            return (
                lambda before: agent_activity_log_dao.list_schedule_history(
                    db.agent.id, SCHEDULE, limit=_PAGE_SIZE, before=before
                ),
                "ix_agent_activity_logs_schedule_history",
                4,
            )
        case "agent-audit":
            return (
                lambda before: audit_log_dao.list_scoped(
                    tenant_id=TENANT, agent_id=db.agent.id, limit=_PAGE_SIZE, before=before
                ),
                "ix_audit_logs_agent_recent",
                2,
            )
        case "tenant-audit":
            return (
                lambda before: audit_log_dao.list_scoped(tenant_id=TENANT, limit=_PAGE_SIZE, before=before),
                "ix_audit_logs_recent",
                2,
            )
        case "platform-audit":
            return (
                lambda before: audit_log_dao.list_scoped(limit=_PAGE_SIZE, before=before),
                "ix_audit_logs_recent",
                1,
            )
        case unreachable:
            assert_never(unreachable)


@pytest.mark.parametrize("view", ["activity", "timeline", "schedule", "agent-audit", "tenant-audit", "platform-audit"])
async def test_each_history_listing_seeks_in_index_order(history: ReceiptDatabase, view: View) -> None:
    list_page, expected_index, id_step = _listing(history, view)
    bound = (AT + timedelta(seconds=_DEEP // 10), UUID(int=_DEEP + 1))
    for before, first in ((None, _FIRST), (bound, _DEEP)):
        page, statements = await dao_statements(lambda before=before: list_page(before))
        ((query, params),) = statements
        async with transaction(history.first):
            # As in the gateway listing test, prove an index supplies the entire order, not that this
            # small test table has the same cost estimates as an installation with millions of rows.
            await history.first.execute("SET LOCAL enable_seqscan = off")
            await history.first.execute("SET LOCAL enable_sort = off")
            await history.first.execute("SET LOCAL enable_incremental_sort = off")
            plan = await plan_of(history, query, params)
        assert not any("Sort" in str(node["Node Type"]) for node in nodes(plan)), outline(plan)
        allowed_indexes = {expected_index}
        if view == "agent-audit":
            # Both indexes provide the tuple order; selectivity determines whether to scan the agent range or
            # filter a small page of the global range.
            allowed_indexes.add("ix_audit_logs_recent")
        scans = [node for node in nodes(plan) if node.get("Index Name") in allowed_indexes]
        assert len(scans) == 1, outline(plan)
        (scan,) = scans
        if before is not None:
            assert "ROW(created_at, id) <" in str(scan.get("Index Cond", "")), outline(plan)
        assert [row.id for row in page] == [UUID(int=first - offset * id_step) for offset in range(_PAGE_SIZE)]
