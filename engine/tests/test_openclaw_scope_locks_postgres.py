from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi import HTTPException
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row

import test_shared_gateway_postgres as shared_fixtures
from app.core.acl_locks import lock_tenants, lock_user
from app.core.domain.users import UserRole
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.user_dao import user_dao
from app.db.connection import DbConnection, Row
from app.db.errors import LockNotAvailableError
from app.db.session import get_connection, transaction
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.openclaw_dispatch import SharedDispatchRequest
from app.records.user import UserRecord
from app.services import chat_submissions, openclaw_gateway_delivery as delivery
from app.services.channels.whatsapp_personal_access import personal_owner
from app.services.channels.zalo_personal_access import zalo_owner
from app.services.openclaw_native_claims import claim_ordinary_turn
from app.services.openclaw_shared_dispatch import claim_ready_shared
from app.services.shared_chat import ensure_canonical_session, read_only_detail
from test_chat_submission_postgres import ReceiptDatabase

receipt_database = shared_fixtures.receipt_database
pooled_database = shared_fixtures.pooled_database

type Operation = Literal["claim", "settle"]
type Table = Literal["tenants", "users", "agents", "chat_sessions"]
type RowLock = Literal["SHARE", "NO KEY UPDATE", "UPDATE"]
type OwnerLocks = Callable[..., Awaitable[tuple[AgentRecord, UserRecord]]]

FINISHED: dict[Operation, str] = {"claim": "delivered", "settle": "completed"}
LOCK_SQL: dict[tuple[Table, RowLock], str] = {
    ("tenants", "SHARE"): "SELECT id FROM tenants WHERE id = %(id)s FOR SHARE NOWAIT",
    ("tenants", "UPDATE"): "SELECT id FROM tenants WHERE id = %(id)s FOR UPDATE NOWAIT",
    ("users", "SHARE"): "SELECT id FROM users WHERE id = %(id)s FOR SHARE NOWAIT",
    ("users", "NO KEY UPDATE"): "SELECT id FROM users WHERE id = %(id)s FOR NO KEY UPDATE NOWAIT",
    ("users", "UPDATE"): "SELECT id FROM users WHERE id = %(id)s FOR UPDATE NOWAIT",
    ("agents", "SHARE"): "SELECT id FROM agents WHERE id = %(id)s FOR SHARE NOWAIT",
    ("agents", "UPDATE"): "SELECT id FROM agents WHERE id = %(id)s FOR UPDATE NOWAIT",
    ("chat_sessions", "UPDATE"): "SELECT id FROM chat_sessions WHERE id = %(id)s FOR UPDATE NOWAIT",
}


@dataclass(slots=True)
class Gate:
    entered: anyio.Event = field(default_factory=anyio.Event)
    release: anyio.Event = field(default_factory=anyio.Event)
    backend_pid: int = 0


@dataclass(frozen=True, slots=True)
class ScopeWorld:
    db: ReceiptDatabase
    monitor: DbConnection
    tenant_id: UUID
    owner: UserRecord
    held: AgentRecord
    sibling: AgentRecord
    other_owner: AgentRecord


async def insert_agent(connection: DbConnection, tenant_id: UUID, owner_id: UUID, name: str) -> AgentRecord:
    agent = AgentRecord(id=uuid4(), name=name, creator_id=owner_id, tenant_id=tenant_id)
    await connection.execute(
        "INSERT INTO agents (id, name, creator_id, tenant_id, role_description, max_triggers, min_poll_interval_min, "
        "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
        "VALUES (%(id)s, %(name)s, %(owner)s, %(tenant)s, '', 20, 5, 5, 'private', 'use', 240)",
        {"id": agent.id, "name": name, "owner": owner_id, "tenant": tenant_id},
    )
    return agent


@pytest.fixture
async def world(pooled_database: ReceiptDatabase) -> AsyncIterator[ScopeWorld]:
    """One tenant with the held agent and a same-owner sibling, plus an agent of a second owner."""
    db = pooled_database
    tenant_id, other_user_id = uuid4(), uuid4()
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO tenants (id, name, slug, tier) VALUES (%(id)s, 'Scope QA', 'scope-qa', 'ultra')",
            {"id": tenant_id},
        )
        await db.first.execute(
            "UPDATE users SET tenant_id = %(tenant)s WHERE id = %(id)s", {"tenant": tenant_id, "id": db.user.id}
        )
        await db.first.execute(
            "INSERT INTO users (id, display_name, tenant_id) VALUES (%(id)s, 'Other owner', %(tenant)s)",
            {"id": other_user_id, "tenant": tenant_id},
        )
        await db.first.execute(
            "UPDATE agents SET tenant_id = %(tenant)s WHERE id = %(id)s", {"tenant": tenant_id, "id": db.agent.id}
        )
        sibling = await insert_agent(db.first, tenant_id, db.user.id, "Sibling")
        other_owner = await insert_agent(db.first, tenant_id, other_user_id, "Other owner")
    async with await AsyncConnection[DictRow].connect(db.conninfo, autocommit=True, row_factory=dict_row) as raw:
        yield ScopeWorld(
            db,
            DbConnection(raw),
            tenant_id,
            replace(db.user, tenant_id=tenant_id),
            replace(db.agent, tenant_id=tenant_id),
            sibling,
            other_owner,
        )


@pytest.fixture
def gates(monkeypatch: pytest.MonkeyPatch) -> dict[UUID, Gate]:
    """Pause the operation of a registered agent after its scope locks are taken, before its writes."""
    registry: dict[UUID, Gate] = {}
    claim_head = gateway_message_dao.claim_ordinary_head
    record_report = delivery.record_report

    async def hold(agent_id: UUID) -> None:
        gate = registry.get(agent_id)
        if gate is None:
            return
        connection = get_connection()
        assert connection is not None
        gate.backend_pid = connection.raw.info.backend_pid
        gate.entered.set()
        await gate.release.wait()

    async def gated_claim(message_id: UUID, agent_id: UUID):
        await hold(agent_id)
        return await claim_head(message_id, agent_id)

    async def gated_report(agent: AgentRecord, message_id: UUID, result: str):
        await hold(agent.id)
        return await record_report(agent, message_id, result)

    monkeypatch.setattr(gateway_message_dao, "claim_ordinary_head", gated_claim)
    monkeypatch.setattr(delivery, "record_report", gated_report)
    return registry


async def seed_message(world: ScopeWorld, agent: AgentRecord, operation: Operation) -> UUID:
    values: dict[str, object] = {"agent_id": agent.id, "content": "hello", "status": "pending"}
    if operation == "settle":
        values |= {"status": "delivered", "delivered_at": datetime.now(UTC)}
    async with transaction(world.db.first):
        return (await gateway_message_dao.create(obj_in=values)).id


async def finish(operation: Operation, agent: AgentRecord, message_id: UUID) -> None:
    if operation == "claim":
        assert await claim_ordinary_turn(agent, message_id) is not None
    else:
        assert await delivery.settle_native_report(agent, message_id, "answer") is not None


async def status_of(world: ScopeWorld, message_id: UUID) -> object:
    return await world.monitor.fetchval("SELECT status FROM gateway_messages WHERE id = %(id)s", {"id": message_id})


async def can_lock(world: ScopeWorld, table: Table, row_id: UUID, mode: RowLock) -> bool:
    try:
        async with transaction(world.db.second):
            await world.db.second.execute(
                LOCK_SQL[table, mode],
                {"id": row_id},
            )
    except LockNotAvailableError:
        return False
    return True


async def waiters_of(world: ScopeWorld, blocker_pid: int) -> list[Row]:
    """Backends reported in a lock wait behind ``blocker_pid``, polled without sleeping until one exists."""
    while True:
        rows = await world.monitor.fetchall(
            "SELECT pid, wait_event_type, query FROM pg_stat_activity "
            "WHERE wait_event_type = 'Lock' AND %(blocker)s = ANY(pg_blocking_pids(pid))",
            {"blocker": blocker_pid},
        )
        if rows:
            return rows
        await anyio.lowlevel.checkpoint()


@pytest.mark.parametrize("table", ["tenants", "users"])
async def test_lock_helpers_are_exclusive_by_default_and_shared_on_request(world: ScopeWorld, table: Table) -> None:
    row_id = world.tenant_id if table == "tenants" else world.owner.id
    for shared, other_share_allowed in ((False, False), (True, True)):
        async with transaction(world.db.first) as holder:
            if table == "tenants":
                await lock_tenants(holder, (world.tenant_id,), shared=shared)
            else:
                await lock_user(holder, world.owner.id, world.tenant_id, shared=shared)
            assert await can_lock(world, table, row_id, "SHARE") is other_share_allowed
            assert not await can_lock(world, table, row_id, "UPDATE")


@pytest.mark.parametrize("owner_locks", [personal_owner, zalo_owner], ids=["whatsapp", "zalo"])
async def test_authorization_only_owner_locks_admit_the_quota_update_but_not_acl_mutators(
    world: ScopeWorld, owner_locks: OwnerLocks
) -> None:
    # Given a transaction holding a personal channel's authorization-only locks on the creator.
    async with transaction(world.db.first):
        await owner_locks(world.held.id, authorization_only=True)
        # Then the creator's quota UPDATE can take its row lock, while an ACL mutator's FOR UPDATE still cannot.
        assert await can_lock(world, "users", world.owner.id, "NO KEY UPDATE")
        assert not await can_lock(world, "users", world.owner.id, "UPDATE")


@pytest.mark.parametrize("operation", ["claim", "settle"])
@pytest.mark.parametrize("neighbour", ["sibling", "other_owner"])
async def test_operations_for_different_agents_in_one_tenant_overlap(
    world: ScopeWorld, gates: dict[UUID, Gate], operation: Operation, neighbour: str
) -> None:
    # Given an operation holding its open transaction with the scope locks taken.
    other = world.sibling if neighbour == "sibling" else world.other_owner
    held_message = await seed_message(world, world.held, operation)
    other_message = await seed_message(world, other, operation)
    gate = gates[world.held.id] = Gate()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(finish, operation, world.held, held_message)
            await gate.entered.wait()
            # Then tenant and owner are shared with other transactions, mutators stay excluded, and the agent is exclusive.
            assert await can_lock(world, "tenants", world.tenant_id, "SHARE")
            assert not await can_lock(world, "tenants", world.tenant_id, "UPDATE")
            assert await can_lock(world, "users", world.owner.id, "SHARE")
            assert not await can_lock(world, "users", world.owner.id, "UPDATE")
            assert not await can_lock(world, "agents", world.held.id, "SHARE")
            # When another agent of the same tenant runs the same operation, it completes while the first stays open.
            with anyio.move_on_after(5) as attempt:
                await finish(operation, other, other_message)
            assert not attempt.cancelled_caught, "the sibling operation blocked behind the open one"
            assert await status_of(world, other_message) == FINISHED[operation]
            assert await status_of(world, held_message) != FINISHED[operation]
            gate.release.set()
    assert await status_of(world, held_message) == FINISHED[operation]


@pytest.mark.parametrize("operation", ["claim", "settle"])
async def test_acl_mutation_waits_for_the_open_operation_then_completes(
    world: ScopeWorld, gates: dict[UUID, Gate], operation: Operation
) -> None:
    # Given an operation holding its open transaction with the scope locks taken.
    message = await seed_message(world, world.held, operation)
    gate = gates[world.held.id] = Gate()
    mutated = anyio.Event()
    mutator_pid = world.db.second.raw.info.backend_pid

    async def mutate() -> None:
        async with transaction(world.db.second):
            await user_dao.update(db_obj=world.owner, obj_in={"is_active": False})
        mutated.set()

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(finish, operation, world.held, message)
            await gate.entered.wait()
            # When the real owner mutation (FOR UPDATE on tenant, then user) arrives, it queues behind the operation.
            tasks.start_soon(mutate)
            waiting = await waiters_of(world, gate.backend_pid)
            assert [(row["pid"], row["wait_event_type"]) for row in waiting] == [(mutator_pid, "Lock")]
            assert "FROM tenants" in str(waiting[0]["query"])
            assert not mutated.is_set()
            # Then it completes only after the operation's transaction ends.
            gate.release.set()
            await mutated.wait()
    assert await status_of(world, message) == FINISHED[operation]
    assert (
        await world.monitor.fetchval("SELECT is_active FROM users WHERE id = %(id)s", {"id": world.owner.id}) is False
    )


async def test_claim_after_a_committing_acl_mutation_observes_it_and_refuses(world: ScopeWorld) -> None:
    # Given an owner mutation holding its tenant and user locks, uncommitted.
    message = await seed_message(world, world.held, "claim")
    mutator_pid = world.db.second.raw.info.backend_pid
    mutated, commit = anyio.Event(), anyio.Event()
    refusals: list[HTTPException] = []

    async def mutate() -> None:
        async with transaction(world.db.second):
            await user_dao.update(db_obj=world.owner, obj_in={"is_active": False})
            mutated.set()
            await commit.wait()

    async def claim() -> None:
        with pytest.raises(HTTPException) as refused:
            await claim_ordinary_turn(world.held, message)
        refusals.append(refused.value)

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(mutate)
            await mutated.wait()
            # When a claim starts, it queues behind the mutation's exclusive tenant lock.
            tasks.start_soon(claim)
            waiting = await waiters_of(world, mutator_pid)
            assert len(waiting) == 1
            assert "FROM tenants" in str(waiting[0]["query"])
            assert not refusals
            commit.set()
    # Then its fresh eligibility check sees the committed change, and nothing was claimed.
    assert [(refused.status_code, refused.detail) for refused in refusals] == [
        (403, {"code": "agent_work_blocked", "blocking_codes": ["owner_inactive"]})
    ]
    assert await status_of(world, message) == "pending"


@dataclass(slots=True)
class LaneSpy:
    """Records the agent and human-lane lock order and can hold an operation right after it locks the lane."""

    gate: Gate | None = None
    order: list[str] = field(default_factory=list)


@pytest.fixture
def lane_spy(monkeypatch: pytest.MonkeyPatch) -> LaneSpy:
    spy = LaneSpy()
    lock_lane = gateway_message_dao.lock_human_lane
    lock_agent = delivery.lock_agent

    async def locked_lane(agent_id: UUID, session_id: UUID) -> bool:
        locked = await lock_lane(agent_id, session_id)
        spy.order.append("lane")
        if spy.gate is not None:
            connection = get_connection()
            assert connection is not None
            spy.gate.backend_pid = connection.raw.info.backend_pid
            spy.gate.entered.set()
            await spy.gate.release.wait()
        return locked

    async def locked_agent(db: DbConnection, agent_id: UUID, tenant_id: UUID | None) -> None:
        spy.order.append("agent")
        await lock_agent(db, agent_id, tenant_id)

    monkeypatch.setattr(gateway_message_dao, "lock_human_lane", locked_lane)
    monkeypatch.setattr(delivery, "lock_agent", locked_agent)
    return spy


async def canonical_turn(world: ScopeWorld, operation: Operation) -> tuple[UUID, UUID]:
    """The owner's canonical session and one turn on its human lane, queued (claim) or claimed (settle)."""
    session = await ensure_canonical_session(world.held.id, world.owner.id)
    values: dict[str, object] = {
        "agent_id": world.held.id,
        "sender_user_id": world.owner.id,
        "conversation_id": str(session.id),
        "content": "hello",
        "status": "pending",
    }
    if operation == "settle":
        values |= {"status": "delivered", "delivered_at": datetime.now(UTC)}
    async with transaction(world.db.first):
        return session.id, (await gateway_message_dao.create(obj_in=values)).id


async def claim_lane(world: ScopeWorld, session_id: UUID, claimed: list[GatewayMessageRecord | None]) -> None:
    claimed.append(await claim_ready_shared(SharedDispatchRequest(world.held.id, datetime.now(UTC), session_id)))


async def test_ensure_canonical_session_finishes_while_a_claim_holds_that_human_lane(
    world: ScopeWorld, lane_spy: LaneSpy
) -> None:
    # Given a claim holding the FOR UPDATE lane lock on the owner's canonical session.
    session_id, message_id = await canonical_turn(world, "claim")
    gate = lane_spy.gate = Gate()
    claimed: list[GatewayMessageRecord | None] = []
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(claim_lane, world, session_id, claimed)
            await gate.entered.wait()
            assert not await can_lock(world, "chat_sessions", session_id, "UPDATE")
            # When the owner's canonical session is ensured concurrently.
            ensured = None
            with anyio.move_on_after(5) as attempt:
                ensured = await ensure_canonical_session(world.held.id, world.owner.id)
            # Then it returns the same row without waiting for the claim, and the claim still completes.
            assert not attempt.cancelled_caught, "ensure blocked behind the claim's human-lane lock"
            assert ensured is not None
            assert ensured.id == session_id
            gate.release.set()
    assert [row.id if row is not None else None for row in claimed] == [message_id]
    assert await status_of(world, message_id) == "delivered"


async def test_canonical_settlement_locks_the_agent_before_the_human_lane(world: ScopeWorld, lane_spy: LaneSpy) -> None:
    # Given a claimed turn on the owner's canonical lane.
    session_id, message_id = await canonical_turn(world, "settle")
    gate = lane_spy.gate = Gate()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            # When its settlement holds the lane lock.
            tasks.start_soon(finish, "settle", world.held, message_id)
            await gate.entered.wait()
            # Then it took shared scope locks and the exclusive agent lock first, the same order as every claim.
            assert lane_spy.order == ["agent", "lane"]
            assert await can_lock(world, "tenants", world.tenant_id, "SHARE")
            assert not await can_lock(world, "tenants", world.tenant_id, "UPDATE")
            assert await can_lock(world, "users", world.owner.id, "SHARE")
            assert not await can_lock(world, "agents", world.held.id, "SHARE")
            assert not await can_lock(world, "chat_sessions", session_id, "UPDATE")
            gate.release.set()
    assert await status_of(world, message_id) == "completed"


async def test_read_only_refusal_behind_a_lane_holder_returns_the_callers_canonical_session(
    world: ScopeWorld, lane_spy: LaneSpy
) -> None:
    # Given a claim holding the owner's canonical lane, and a company admin who may read it but not write to it.
    session_id, message_id = await canonical_turn(world, "claim")
    admin = UserRecord(id=uuid4(), tenant_id=world.tenant_id, display_name="Org admin", role=UserRole.ORG_ADMIN)
    async with transaction(world.db.first):
        await world.db.first.execute(
            "INSERT INTO users (id, display_name, tenant_id, role) VALUES (%(id)s, 'Org admin', %(tenant)s, 'org_admin')",
            {"id": admin.id, "tenant": world.tenant_id},
        )
    gate = lane_spy.gate = Gate()
    claimed: list[GatewayMessageRecord | None] = []
    refusals: list[HTTPException] = []

    async def submit() -> None:
        with pytest.raises(HTTPException) as refused:
            _ = await chat_submissions.accept_submission(
                chat_submissions.ChatSubmission(client_submission_id=uuid4(), content="not mine"),
                user=admin,
                agent=world.held,
                session_id=session_id,
                history=[],
            )
        refusals.append(refused.value)

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(claim_lane, world, session_id, claimed)
            await gate.entered.wait()
            # When the admin submits into that session, the submission queues behind the lane lock.
            tasks.start_soon(submit)
            waiting = await waiters_of(world, gate.backend_pid)
            assert len(waiting) == 1
            assert "FROM chat_sessions" in str(waiting[0]["query"])
            assert not refusals
            gate.release.set()
    # Then the refusal names the admin's own canonical session, and the claim completed.
    canonical = await world.monitor.fetchval(
        "SELECT id FROM chat_sessions WHERE agent_id = %(agent)s AND user_id = %(user)s "
        "AND source_channel = 'web' AND is_primary AND NOT is_group",
        {"agent": world.held.id, "user": admin.id},
    )
    assert isinstance(canonical, UUID)
    assert canonical != session_id
    assert [(refused.status_code, refused.detail) for refused in refusals] == [(409, read_only_detail(canonical))]
    assert [row.id if row is not None else None for row in claimed] == [message_id]
    assert await status_of(world, message_id) == "delivered"
