from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

import anyio
import pytest
from tests.test_chat_submission_postgres import receipt_db
from tests.test_whatsapp_personal_burst_store_fixtures import (
    admit,
    admit_in_transaction,
    age,
    anchor,
    batch_of,
    burst_db,
    members_of,
    present,
    receive,
    receive_committed,
    usage_of,
    wait_until_second_blocked,
)

from app.dao.personal_burst_dao import personal_burst_dao
from app.dao.personal_burst_dispatch_dao import personal_burst_dispatch_dao
from app.dao.personal_burst_input_dao import personal_burst_input_dao
from app.db.errors import CheckViolationError, ForeignKeyViolationError, UniqueViolationError
from app.db.session import transaction
from app.records.personal_burst import PersonalBurstBatch
from app.records.personal_burst_errors import (
    PersonalBurstAnchorError,
    PersonalBurstLimitError,
    PersonalBurstTransactionRequiredError,
)
from app.records.personal_burst_input import NewBurstMember, ReceiptKey, RejectedOutcome

__all__ = ["burst_db", "receipt_db"]


async def test_first_arrival_reserves_anchor_header_and_member(burst_db):
    # Given/When the first original arrives.
    f = burst_db
    batch = await admit(f, "n1", "hello")
    # Then one pending header pins the full scope with quiet-window readiness and one ordinal-1 member.
    assert batch.scope == f.scope
    assert (batch.state, batch.member_count, batch.member_bytes, batch.body_bytes) == ("pending", 1, 5, 5)
    assert batch.ready_at - batch.first_at == timedelta(seconds=2)
    assert batch.frozen_body is None
    members = await members_of(f, batch.gateway_id)
    assert [(m.ordinal, m.text, m.outcome) for m in members] == [(1, "hello", "accepted")]
    assert members[0].chat_message_id is not None


async def test_append_extends_quiet_window_but_never_past_max(burst_db):
    # Given a batch whose first member is 4 seconds old while the last arrival is current.
    f = burst_db
    first = await admit(f, "n1", "a")
    async with transaction(f.db.first):
        await f.db.first.execute(
            "UPDATE personal_burst_batches SET first_at = first_at - interval '4 seconds' WHERE gateway_id = %(id)s",
            {"id": first.gateway_id},
        )
    # When a second original arrives inside the quiet window, ready_at is min(first+max, last+quiet).
    second = await admit(f, "n2", "b")
    assert second.gateway_id == first.gateway_id
    assert second.ready_at == min(second.first_at + timedelta(seconds=5), second.last_at + timedelta(seconds=2))
    assert second.ready_at == second.first_at + timedelta(seconds=5)
    assert (second.member_count, second.member_bytes, second.body_bytes) == (2, 2, 4)


async def test_append_refused_after_ready_and_new_batch_follows(burst_db):
    # Given a batch whose quiet window has elapsed.
    f = burst_db
    first = await admit(f, "n1", "a")
    await age(f, first.gateway_id, 2.5)
    # When the next original arrives, Then it opens a later batch instead of changing the due one.
    second = await admit(f, "n2", "b")
    assert second.gateway_id != first.gateway_id
    assert present(await batch_of(f, first.gateway_id)).member_count == 1


async def test_tail_barrier_web_row_and_foreign_scope_refuse_append(burst_db):
    # Given a pending batch followed by a different sender's shared row.
    f = burst_db
    first = await admit(f, "n1", "a")
    async with transaction(f.db.first):
        await anchor(f, sender=f.db.user.id, content="web turn")
    # When the next original arrives, Then the actual FIFO tail is not a batch and a later batch is created.
    second = await admit(f, "n2", "b")
    assert second.gateway_id != first.gateway_id
    # And a differing generation never joins even the eligible tail.
    other = replace(f.scope, generation=uuid4())
    async with transaction(f.db.first):
        member = await receive(f, "n3", "c")
        assert await personal_burst_dao.append_member(other, member, f.limits) is None
        assert await personal_burst_dao.append_member(f.scope, member, f.limits) is not None


async def test_body_limit_counts_utf8_and_separator_exactly(burst_db):
    # Given a 9 byte limit and "é" (2 bytes): 2 + 2 + 2 + 2 + 2 + 1 separators needs the exact count.
    f = burst_db
    limits = replace(f.limits, batch_bytes=6, pending_bytes=6)
    first = await admit(f, "n1", "é", limits=limits)
    second = await admit(f, "n2", "é", limits=limits)  # 2 + 2 + 2 separator bytes = 6: exactly fits
    assert second.gateway_id == first.gateway_id
    assert second.body_bytes == 6
    third = await admit(f, "n3", "a", limits=limits)  # 6 + 2 + 1 = 9 > 6
    assert third.gateway_id != first.gateway_id
    # And a single configured-oversize original is a durable explicit refusal, never truncated.
    async with transaction(f.db.first):
        member = await receive(f, "n4", "éééé")
        with pytest.raises(PersonalBurstLimitError):
            await personal_burst_dao.create_batch(await anchor(f), f.scope, member, limits)


async def test_maximum_original_fits_default_batch_alone(burst_db):
    f = burst_db
    batch = await admit(f, "n1", "😀" * 4096)
    assert batch.body_bytes == 16384 == f.limits.batch_bytes


async def test_pending_usage_counts_originals_legacy_personal_and_excludes_inflight(burst_db):
    f = burst_db
    first = await admit(f, "n1", "ab")
    await admit(f, "n2", "cde")
    async with transaction(f.db.first):
        legacy = await anchor(f, content="legacy!")  # personal legacy: report intent to the Personal reply session
        await f.db.first.execute(
            "INSERT INTO gateway_report_deliveries (id, message_id, destination_kind, destination_id) "
            "VALUES (%(id)s, %(m)s, 'im', %(d)s)",
            {"id": uuid4(), "m": legacy, "d": f.scope.reply_session_id},
        )
        await anchor(f, content="web turn is not Personal")
    usage = await usage_of(f)
    assert (usage.count, usage.bytes) == (3, 2 + 3 + 7)
    # When the batch becomes the in-flight claimed turn, Then it stops counting against the backlog.
    await age(f, first.gateway_id, 10)
    async with transaction(f.db.first):
        await personal_burst_dispatch_dao.seal(first.gateway_id)
        assert await personal_burst_dispatch_dao.mark_claimed(first.gateway_id)
    usage = await usage_of(f)
    assert (usage.count, usage.bytes) == (1, 7)


@pytest.mark.parametrize("status", ["pending", "delivered", "completed"])
async def test_next_created_at_is_strictly_after_the_tail_even_in_the_future(burst_db, status):
    f = burst_db
    future = datetime.now().astimezone() + timedelta(hours=1)
    async with transaction(f.db.first):
        gateway_id = await anchor(f, at=future)
        await f.db.first.execute(
            "UPDATE gateway_messages SET status = %(status)s WHERE id = %(id)s",
            {"status": status, "id": gateway_id},
        )
        assert await personal_burst_dao.next_created_at(f.scope.agent_id, f.scope.shared_session_id) == (
            future + timedelta(microseconds=1)
        )


async def test_outcome_lookup_distinguishes_accepted_rejected_and_legacy(burst_db):
    f = burst_db
    batch = await admit(f, "n1", "hello")
    rejections: tuple[tuple[str, RejectedOutcome], ...] = (
        ("r1", "capacity_rejected"),
        ("r2", "quota_rejected"),
        ("r3", "oversize_rejected"),
    )
    async with transaction(f.db.first):
        for key, outcome in rejections:
            member = await receive(f, key, "body that is never retained")
            stored = await personal_burst_input_dao.record_rejection(member.receipt, outcome)
            assert (stored.outcome, stored.text, stored.text_bytes, stored.gateway_id, stored.ordinal) == (
                outcome,
                None,
                None,
                None,
                None,
            )
        legacy = await receive(f, "old", "legacy accepted")  # receipt without side outcome
    async with transaction(f.db.second):
        accepted = await personal_burst_input_dao.find_outcome(ReceiptKey(f.scope.config_id, "whatsapp", "n1"))
        rejected = await personal_burst_input_dao.find_outcome(ReceiptKey(f.scope.config_id, "whatsapp", "r2"))
        missing = await personal_burst_input_dao.find_outcome(legacy.receipt)
    assert accepted is not None
    assert (accepted.outcome, accepted.gateway_id, accepted.ordinal) == ("accepted", batch.gateway_id, 1)
    assert rejected is not None
    assert rejected.outcome == "quota_rejected"
    assert missing is None
    # And a second outcome for one native receipt is a unique violation, never a second row.
    with pytest.raises(UniqueViolationError):
        async with transaction(f.db.first):
            await personal_burst_input_dao.record_rejection(rejected.receipt, "capacity_rejected")


async def test_constraints_reject_inconsistent_member_and_rejection_shapes(burst_db):
    f = burst_db
    batch = await admit(f, "n1", "hello")
    base = "INSERT INTO personal_burst_inputs (id, config_id, provider, event_key, outcome, batch_id, ordinal, text, text_bytes) "
    cases = [
        ("'accepted', NULL, NULL, NULL, NULL", CheckViolationError),  # accepted without anchor/text
        ("'quota_rejected', %(b)s, 2, 'x', 1", CheckViolationError),  # rejection retaining a body
        ("'accepted', %(b)s, 2, 'abc', 99", CheckViolationError),  # byte count must equal octet_length
        ("'accepted', %(b)s, 1, 'dup', 3", UniqueViolationError),  # (batch, ordinal) is unique
    ]
    for index, (values, error) in enumerate(cases):
        member = await receive_committed(f, f"c{index}")
        with pytest.raises(error):
            async with transaction(f.db.first):
                await f.db.first.execute(
                    base + f"VALUES (%(id)s, %(c)s, 'whatsapp', %(k)s, {values})",
                    {"id": uuid4(), "c": f.scope.config_id, "k": member.receipt.event_key, "b": batch.gateway_id},
                )
    # And an outcome row cannot exist without its reserved native receipt (composite FK, no pretend UUID).
    with pytest.raises(ForeignKeyViolationError):
        async with transaction(f.db.first):
            await personal_burst_input_dao.record_rejection(
                ReceiptKey(f.scope.config_id, "whatsapp", "never"), "quota_rejected"
            )


async def test_mutations_require_owning_transaction_and_existing_anchor(burst_db):
    f = burst_db
    member = NewBurstMember(ReceiptKey(f.scope.config_id, "whatsapp", "n"), "a", None)
    with pytest.raises(PersonalBurstTransactionRequiredError):
        await personal_burst_dao.append_member(f.scope, member, f.limits)
    with pytest.raises(PersonalBurstTransactionRequiredError):
        await personal_burst_dao.create_batch(uuid4(), f.scope, member, f.limits)
    with pytest.raises(PersonalBurstTransactionRequiredError):
        await personal_burst_dao.next_created_at(f.scope.agent_id, f.scope.shared_session_id)
    # And a batch cannot be anchored on a missing or non-tail gateway row.
    async with transaction(f.db.first):
        received = await receive(f, "n", "a")
        with pytest.raises(PersonalBurstAnchorError):
            await personal_burst_dao.create_batch(uuid4(), f.scope, received, f.limits)


async def test_rollback_discards_header_members_and_receipt_together(burst_db):
    f = burst_db
    await admit(f, "n1", "a")

    async def admit_then_fail():
        async with transaction(f.db.first):
            await admit_in_transaction(f, "n2", "b", f.limits)
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await admit_then_fail()
    async with transaction(f.db.first):
        counts = await f.db.first.fetchone(
            "SELECT (SELECT member_count FROM personal_burst_batches) AS members, "
            "(SELECT count(*) FROM channel_event_receipts) AS receipts, (SELECT count(*) FROM personal_burst_inputs) AS inputs"
        )
    assert counts == {"members": 1, "receipts": 1, "inputs": 1}


async def test_concurrent_appends_serialize_on_the_shared_parent_lock(burst_db):
    # Given a batch and a first transaction that appended but has not committed.
    f = burst_db
    first = await admit(f, "n1", "a")
    locked, release = anyio.Event(), anyio.Event()
    result: list[PersonalBurstBatch] = []

    async def holder():
        async with transaction(f.db.first):
            await admit_in_transaction(f, "n2", "b", f.limits)
            locked.set()
            await release.wait()

    async def contender():
        await locked.wait()
        async with transaction(f.db.second):
            result.append(await admit_in_transaction(f, "n3", "c", f.limits))

    # When a second connection appends, Then PostgreSQL blocks it on the parent lock until the first commits.
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(holder)
            tasks.start_soon(contender)
            await locked.wait()
            await wait_until_second_blocked(f)
            release.set()
    assert result[0].gateway_id == first.gateway_id
    members = await members_of(f, first.gateway_id)
    assert [(m.ordinal, m.text) for m in members] == [(1, "a"), (2, "b"), (3, "c")]
