"""Per-agent daily message cap against live PostgreSQL: policy, concurrency and every charge site's rollback."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from tests.channel_submission_fixtures import ChannelDatabase, channel_db
from tests.daily_message_clock import FAR_EAST, TODAY, frozen_clock
from tests.personal_burst_fixtures import LinkedBurst, linked, scalar, submit
from tests.test_chat_submission_postgres import ReceiptDatabase, receipt_db, writable_receipt_db

from app.core.json_types import date_from_row, int_from_row
from app.dao.agent_daily_message_usage_dao import agent_daily_message_usage_dao
from app.db.connection import DbConnection
from app.db.errors import CheckViolationError, ForeignKeyViolationError
from app.db.session import transaction
from app.records.organization_plan import OrganizationPlanId
from app.services import agent_message_quota as quota
from app.services.channels.personal_burst_outcome import PersonalAccepted, PersonalRejected
from app.services.channels.submissions import accept_channel_event
from app.services.chat_submissions import ChatSubmission, accept_submission
from app.services.quota_guard import QuotaExceeded
from tier_limit_fixtures import approve_admission_for

__all__ = ["channel_db", "frozen_clock", "linked", "receipt_db", "writable_receipt_db"]

PRO_LIMIT = 100


async def _govern(
    db: ReceiptDatabase,
    tier: OrganizationPlanId | None = OrganizationPlanId.PRO,
    *,
    system: bool = False,
    timezone: str | None = None,
) -> UUID:
    """Put the fixture agent in an organization (tier-governed when ``tier`` is given), maybe with a genesis admin."""
    tenant_id = uuid4()
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'Cap QA', %(slug)s)",
            {"id": tenant_id, "slug": f"cap-{tenant_id.hex[:8]}"},
        )
        await db.first.execute("UPDATE users SET tenant_id = %(tenant)s", {"tenant": tenant_id})
        await db.first.execute(
            "UPDATE agents SET tenant_id = %(tenant)s, is_system = %(system)s WHERE id = %(id)s",
            {"tenant": tenant_id, "system": system, "id": db.agent.id},
        )
        if tier is not None:
            _ = await approve_admission_for(db.first, tenant_id, tier)
        if timezone is not None:
            await db.first.execute(
                "INSERT INTO users (id, display_name, tenant_id, role, is_genesis, timezone) "
                "VALUES (%(id)s, 'Genesis', %(tenant)s, 'org_admin', true, %(timezone)s)",
                {"id": uuid4(), "tenant": tenant_id, "timezone": timezone},
            )
    return tenant_id


async def _tier_channel_agent(f: ChannelDatabase) -> None:
    async with transaction(f.db.first):
        _ = await approve_admission_for(f.db.first, f.tenant_id, OrganizationPlanId.PRO)


async def _set_usage(db: ReceiptDatabase, used: int, usage_date: date = TODAY) -> None:
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO agent_daily_message_usage (agent_id, usage_date, messages_used) "
            "VALUES (%(id)s, %(day)s, %(used)s) ON CONFLICT (agent_id) DO UPDATE "
            "SET usage_date = EXCLUDED.usage_date, messages_used = EXCLUDED.messages_used",
            {"id": db.agent.id, "day": usage_date, "used": used},
        )


async def _usage(db: ReceiptDatabase) -> tuple[date, int] | None:
    async with transaction(db.first):
        row = await db.first.fetchone(
            "SELECT usage_date, messages_used FROM agent_daily_message_usage WHERE agent_id = %(id)s",
            {"id": db.agent.id},
        )
    return None if row is None else (date_from_row(row["usage_date"]), int_from_row(row["messages_used"]))


async def _charge(db: ReceiptDatabase, tenant_id: UUID, *, system: bool = False) -> None:
    async with transaction(db.first):
        await quota.charge_agent_daily_message(replace(db.agent, tenant_id=tenant_id, is_system=system))


async def test_pro_agent_accepts_one_hundred_messages_and_refuses_the_next(receipt_db: ReceiptDatabase):
    db = receipt_db
    tenant_id = await _govern(db)

    async with transaction(db.first):
        agent = replace(db.agent, tenant_id=tenant_id)
        for _ in range(PRO_LIMIT):
            await quota.charge_agent_daily_message(agent)
    with pytest.raises(QuotaExceeded) as refused:
        await _charge(db, tenant_id)

    assert refused.value.quota_type == "agent_daily_messages"
    assert (
        refused.value.message == "This agent has reached its daily limit of 100 messages. It resets at midnight (UTC)."
    )
    assert await _usage(db) == (TODAY, PRO_LIMIT)


@pytest.mark.parametrize(
    ("tier", "limit"), [(OrganizationPlanId.MAX, 200), (OrganizationPlanId.ULTRA, 500)], ids=["max", "ultra"]
)
async def test_larger_tiers_admit_up_to_their_own_limit(
    receipt_db: ReceiptDatabase, tier: OrganizationPlanId, limit: int
):
    db = receipt_db
    tenant_id = await _govern(db, tier)
    await _set_usage(db, limit - 1)

    await _charge(db, tenant_id)
    with pytest.raises(QuotaExceeded, match=rf"limit of {limit} messages"):
        await _charge(db, tenant_id)

    assert await _usage(db) == (TODAY, limit)


async def test_untiered_agent_is_never_refused_or_counted(receipt_db: ReceiptDatabase):
    db = receipt_db
    tenant_id = await _govern(db, None)

    for _ in range(PRO_LIMIT + 1):
        await _charge(db, tenant_id)

    assert await _usage(db) is None


async def test_system_agent_of_a_tiered_organization_is_never_refused_or_counted(receipt_db: ReceiptDatabase):
    db = receipt_db
    tenant_id = await _govern(db, system=True)
    await _set_usage(db, PRO_LIMIT)

    await _charge(db, tenant_id, system=True)

    assert await _usage(db) == (TODAY, PRO_LIMIT)


async def test_an_earlier_local_date_counts_from_zero(receipt_db: ReceiptDatabase):
    db = receipt_db
    tenant_id = await _govern(db)
    await _set_usage(db, PRO_LIMIT, TODAY - timedelta(days=1))

    await _charge(db, tenant_id)

    assert await _usage(db) == (TODAY, 1)


async def test_the_day_is_the_genesis_admin_local_date_not_the_utc_date(receipt_db: ReceiptDatabase):
    db = receipt_db
    tenant_id = await _govern(db, timezone=FAR_EAST)
    local_date = TODAY + timedelta(days=1)

    # A full counter on the UTC date is a different local day, so the first message starts a new count.
    await _set_usage(db, PRO_LIMIT, TODAY)
    await _charge(db, tenant_id)
    assert await _usage(db) == (local_date, 1)

    # A full counter on the local date is refused, and the message names the admin's timezone.
    await _set_usage(db, PRO_LIMIT, local_date)
    with pytest.raises(QuotaExceeded, match=rf"midnight \({FAR_EAST}\)"):
        await _charge(db, tenant_id)


@pytest.mark.parametrize("timezone", ["", "Not/AZone"], ids=["empty", "invalid"])
async def test_unusable_genesis_timezone_counts_the_utc_day(receipt_db: ReceiptDatabase, timezone: str):
    db = receipt_db
    tenant_id = await _govern(db, timezone=timezone)
    await _set_usage(db, PRO_LIMIT - 1)

    await _charge(db, tenant_id)
    with pytest.raises(QuotaExceeded, match=r"midnight \(UTC\)"):
        await _charge(db, tenant_id)

    assert await _usage(db) == (TODAY, PRO_LIMIT)


@pytest.mark.parametrize("stored", ["today", "yesterday", "none"])
async def test_concurrent_charges_cannot_exceed_the_limit(
    receipt_db: ReceiptDatabase, monkeypatch: pytest.MonkeyPatch, stored: str
):
    # Given an agent whose counter already holds the whole limit today, yesterday, or has no row at all.
    db = receipt_db
    limit = 7
    if stored != "none":
        await _set_usage(db, limit, TODAY if stored == "today" else TODAY - timedelta(days=1))

    @asynccontextmanager
    async def committed_session() -> AsyncGenerator[DbConnection]:
        async with await AsyncConnection[DictRow].connect(db.conninfo, autocommit=True, row_factory=dict_row) as raw:
            yield DbConnection(raw)

    monkeypatch.setattr(agent_daily_message_usage_dao, "session", committed_session)

    # When independent committed connections race for the remaining budget.
    results = await asyncio.gather(
        *(agent_daily_message_usage_dao.charge(db.agent.id, TODAY, limit) for _ in range(24))
    )

    # Then exactly the remaining budget is admitted, each unit once, and the counter stops at the limit.
    admitted = sorted(count for count in results if count is not None)
    assert admitted == ([] if stored == "today" else list(range(1, limit + 1)))
    assert await _usage(db) == (TODAY, limit)


async def test_refused_web_message_rolls_back_the_whole_turn(writable_receipt_db: ReceiptDatabase):
    # Given an agent at its daily limit in a tier-governed organization.
    db = writable_receipt_db
    tenant_id = await _govern(db)
    await _set_usage(db, PRO_LIMIT)

    # When a member submits another message.
    with pytest.raises(QuotaExceeded) as refused:
        async with transaction(db.first):
            _ = await accept_submission(
                ChatSubmission(client_submission_id=uuid4(), content="one too many"),
                user=replace(db.user, tenant_id=tenant_id),
                agent=replace(db.agent, tenant_id=tenant_id),
                session_id=db.session_id,
                history=[],
            )

    # Then the agent-level refusal leaves no message, queue row, user charge or agent charge.
    assert refused.value.quota_type == "agent_daily_messages"
    async with transaction(db.first):
        left = await db.first.fetchone(
            "SELECT (SELECT count(*) FROM chat_messages) AS messages, (SELECT count(*) FROM gateway_messages) AS queued, "
            "(SELECT quota_messages_used FROM users WHERE id = %(user)s) AS charged",
            {"user": db.user.id},
        )
    assert left == {"messages": 0, "queued": 0, "charged": 0}
    assert await _usage(db) == (TODAY, PRO_LIMIT)


async def test_accepted_web_message_charges_only_the_agent_once_and_replay_charges_nothing(
    writable_receipt_db: ReceiptDatabase,
):
    db = writable_receipt_db
    tenant_id = await _govern(db)
    submission = ChatSubmission(client_submission_id=uuid4(), content="hello")

    for expected_replay in (False, True):
        async with transaction(db.first):
            accepted = await accept_submission(
                submission,
                user=replace(db.user, tenant_id=tenant_id),
                agent=replace(db.agent, tenant_id=tenant_id),
                session_id=db.session_id,
                history=[],
            )
        assert accepted.replayed is expected_replay

    async with transaction(db.first):
        charged = await db.first.fetchval("SELECT quota_messages_used FROM users WHERE id = %(id)s", {"id": db.user.id})
    assert (charged, await _usage(db)) == (0, (TODAY, 1))


@pytest.mark.parametrize(
    ("tier", "admitted"), [(OrganizationPlanId.PRO, True), (None, False)], ids=["tiered-admits", "untiered-refuses"]
)
async def test_exhausted_per_user_quota_only_refuses_in_an_untiered_organization(
    writable_receipt_db: ReceiptDatabase, tier: OrganizationPlanId | None, admitted: bool
):
    # Given a member whose stored permanent allowance is spent, in an organization with or without a tier.
    db = writable_receipt_db
    tenant_id = await _govern(db, tier)
    async with transaction(db.first):
        await db.first.execute(
            "UPDATE users SET quota_message_limit = 50, quota_message_period = 'permanent', quota_messages_used = 50 "
            "WHERE id = %(id)s",
            {"id": db.user.id},
        )

    async def submit() -> None:
        async with transaction(db.first):
            _ = await accept_submission(
                ChatSubmission(client_submission_id=uuid4(), content="after the allowance"),
                user=replace(db.user, tenant_id=tenant_id),
                agent=replace(db.agent, tenant_id=tenant_id),
                session_id=db.session_id,
                history=[],
            )

    # When the member sends another message.
    if admitted:
        await submit()
    else:
        with pytest.raises(QuotaExceeded) as refused:
            await submit()
        assert refused.value.quota_type == "conversation"

    # Then only the agent's daily counter moves in a tiered organization, and the stored allowance never does.
    async with transaction(db.first):
        charged = await db.first.fetchval("SELECT quota_messages_used FROM users WHERE id = %(id)s", {"id": db.user.id})
    assert charged == 50
    assert await _usage(db) == ((TODAY, 1) if admitted else None)


async def test_refused_channel_event_rolls_back_the_receipt(channel_db: ChannelDatabase):
    # Given a channel agent at its daily limit.
    db = channel_db.db
    await _tier_channel_agent(channel_db)
    await _set_usage(db, PRO_LIMIT)

    # When another native event arrives.
    with pytest.raises(QuotaExceeded) as refused:
        async with transaction(db.first):
            _ = await accept_channel_event(channel_db.event)

    # Then nothing of it persists, so the provider's redelivery is judged afresh.
    assert refused.value.quota_type == "agent_daily_messages"
    async with transaction(db.first):
        left = await db.first.fetchone(
            "SELECT (SELECT count(*) FROM channel_event_receipts) AS receipts, "
            "(SELECT quota_messages_used FROM users WHERE id = %(sender)s) AS charged",
            {"sender": channel_db.sender_id},
        )
    assert left == {"receipts": 0, "charged": 0}

    # And once the agent has room the same event is accepted and charges the agent, not the sender's own allowance.
    await _set_usage(db, PRO_LIMIT - 1)
    async with transaction(db.first):
        _ = await accept_channel_event(channel_db.event)
        charged = await db.first.fetchval(
            "SELECT quota_messages_used FROM users WHERE id = %(sender)s", {"sender": channel_db.sender_id}
        )
    assert (charged, await _usage(db)) == (0, (TODAY, PRO_LIMIT))


async def test_personal_original_over_the_agent_limit_is_a_quota_rejection_that_charges_nobody(linked: LinkedBurst):
    # Given a Personal WhatsApp agent at its daily limit.
    db = linked.f.db
    await _tier_channel_agent(linked.f)
    await _set_usage(db, PRO_LIMIT)

    # When its owner sends another self message.
    rejected = await submit(linked, "n1", "over the limit")

    # Then it is the existing quota rejection, and no message or sender charge is left behind.
    assert isinstance(rejected.personal, PersonalRejected)
    assert rejected.personal.outcome == "quota_rejected"
    assert await scalar(linked, "SELECT quota_messages_used FROM users WHERE id = %(id)s", {"id": db.user.id}) == 0
    assert await scalar(linked, "SELECT count(*) FROM chat_messages WHERE role = 'user'") == 0
    assert await _usage(db) == (TODAY, PRO_LIMIT)

    # And with room left the next message is admitted and charges the agent, not the sender's own allowance.
    await _set_usage(db, PRO_LIMIT - 1)
    admitted = await submit(linked, "n2", "within the limit")
    assert isinstance(admitted.personal, PersonalAccepted)
    assert await scalar(linked, "SELECT quota_messages_used FROM users WHERE id = %(id)s", {"id": db.user.id}) == 0
    assert await _usage(db) == (TODAY, PRO_LIMIT)


@pytest.mark.parametrize(
    ("own_agent", "count", "violation"),
    [(False, 1, ForeignKeyViolationError), (True, -1, CheckViolationError)],
    ids=["unknown-agent", "negative-count"],
)
async def test_counter_rows_belong_to_an_agent_and_never_go_negative(
    receipt_db: ReceiptDatabase, own_agent: bool, count: int, violation: type[Exception]
):
    db = receipt_db

    with pytest.raises(violation):
        async with transaction(db.first):
            await db.first.execute(
                "INSERT INTO agent_daily_message_usage (agent_id, usage_date, messages_used) "
                "VALUES (%(agent)s, %(day)s, %(count)s)",
                {"agent": db.agent.id if own_agent else uuid4(), "day": TODAY, "count": count},
            )
