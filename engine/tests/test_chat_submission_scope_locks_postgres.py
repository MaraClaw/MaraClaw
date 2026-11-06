from uuid import UUID, uuid4

import anyio
import pytest
from fastapi import HTTPException

import test_openclaw_scope_locks_postgres as scope_fixtures
from app.dao.user_dao import user_dao
from app.db.errors import DbError, LockNotAvailableError
from app.db.session import transaction
from app.records.user import UserRecord
from app.services import chat_submissions
from app.services.chat_submissions import AcceptedSubmission, ChatSubmission
from app.services.openclaw_native_claims import claim_ordinary_turn
from test_openclaw_scope_locks_postgres import FINISHED, Gate, Operation, ScopeWorld, finish, seed_message, waiters_of

receipt_database = scope_fixtures.receipt_database
pooled_database = scope_fixtures.pooled_database
world = scope_fixtures.world
gates = scope_fixtures.gates


@pytest.mark.parametrize("operation", ["claim", "settle"])
async def test_submission_quota_and_native_scope_locks_complete_without_a_cycle(
    world: ScopeWorld, monkeypatch: pytest.MonkeyPatch, operation: Operation
) -> None:
    # Given a submission paused after its receipt INSERT has taken the agent FK KEY SHARE lock.
    message_id = await seed_message(world, world.held, operation)
    async with transaction(world.db.first):
        await world.db.first.execute(
            "UPDATE chat_sessions SET is_group = TRUE WHERE id = %(id)s", {"id": world.db.session_id}
        )
    reserved, charge = anyio.Event(), anyio.Event()
    original_charge = chat_submissions.charge_conversation_quota
    accepted: list[AcceptedSubmission] = []
    failures: list[DbError] = []
    submitter_pid = world.db.second.raw.info.backend_pid

    async def paused_charge(user: UserRecord) -> None:
        reserved.set()
        await charge.wait()
        await original_charge(user)

    async def submit() -> None:
        try:
            async with transaction(world.db.second):
                accepted.append(
                    await chat_submissions.accept_submission(
                        ChatSubmission(client_submission_id=uuid4(), content="concurrent submission"),
                        user=world.owner,
                        agent=world.held,
                        session_id=world.db.session_id,
                        history=[],
                    )
                )
        except DbError as error:
            failures.append(error)

    async def native() -> None:
        try:
            await finish(operation, world.held, message_id)
        except DbError as error:
            failures.append(error)

    monkeypatch.setattr(chat_submissions, "charge_conversation_quota", paused_charge)
    # When a real claim/settlement queues behind that transaction before the quota UPDATE proceeds.
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(submit)
            await reserved.wait()
            tasks.start_soon(native)
            waiting = await waiters_of(world, submitter_pid)
            assert len(waiting) == 1
            charge.set()

    # Then both operations committed, with exactly one accepted turn and one quota charge.
    assert not failures, [str(error) for error in failures]
    assert len(accepted) == 1
    assert accepted[0].replayed is False
    counts = await world.monitor.fetchone(
        "SELECT (SELECT count(*) FROM chat_messages WHERE role = 'user') AS receipts, "
        "(SELECT count(*) FROM gateway_messages WHERE status = 'pending') AS queued, "
        "(SELECT quota_messages_used FROM users WHERE id = %(user)s) AS charged, "
        "(SELECT status FROM gateway_messages WHERE id = %(message)s) AS native_status",
        {"user": world.owner.id, "message": message_id},
    )
    assert counts == {"receipts": 1, "queued": 1, "charged": 1, "native_status": FINISHED[operation]}


@pytest.mark.parametrize("operation", ["claim", "settle"])
async def test_identity_attachment_waits_for_native_scope_and_blocks_later_work(
    world: ScopeWorld, gates: dict[UUID, Gate], operation: Operation
) -> None:
    identity_id = uuid4()
    async with transaction(world.db.first):
        await world.db.first.execute(
            "INSERT INTO identities (id, is_active) VALUES (%(id)s, FALSE)", {"id": identity_id}
        )
    message_id = await seed_message(world, world.held, operation)
    later_message_id = await seed_message(world, world.held, "claim")
    gate = gates[world.held.id] = Gate()
    blocked = False

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(finish, operation, world.held, message_id)
            await gate.entered.wait()
            try:
                async with transaction(world.db.second):
                    await world.db.second.execute("SET LOCAL lock_timeout = '200ms'")
                    await user_dao.update(db_obj=world.owner, obj_in={"identity_id": identity_id})
            except LockNotAvailableError:
                blocked = True
            finally:
                gate.release.set()

    assert blocked, "identity attachment committed while the native operation held its authorization scope"
    async with transaction(world.db.second):
        await user_dao.update(db_obj=world.owner, obj_in={"identity_id": identity_id})
    with pytest.raises(HTTPException) as refused:
        await claim_ordinary_turn(world.held, later_message_id)
    assert (refused.value.status_code, refused.value.detail) == (
        403,
        {"code": "agent_work_blocked", "blocking_codes": ["identity_inactive"]},
    )
    assert await scope_fixtures.status_of(world, message_id) == FINISHED[operation]
    assert await scope_fixtures.status_of(world, later_message_id) == "pending"
