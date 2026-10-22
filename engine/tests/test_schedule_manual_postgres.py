import asyncio
from contextvars import Context
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import final
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api import schedules
from app.dao.activity_log_dao import agent_activity_log_dao
from app.dao.schedule_dao import agent_schedule_dao
from app.dao.schedule_manual_run_dao import schedule_manual_run_dao as runs
from app.db.session import connection_ctx
from app.records.schedule import AgentScheduleRecord
from test_schedule_occurrence_postgres import (
    accept_claim,
    live_schedule as live_schedule,
    manual_schedule as manual_schedule,
    schedule_app,
)


@final
class CommitObserver:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.visible: list[int] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def independent_count() -> int:
            async with connection_ctx() as db:
                row = await db.fetchone("SELECT count(*) AS count FROM agent_schedule_manual_runs")
                assert row is not None
                return int(str(row["count"]))

        async def observe(message: Message) -> None:
            if message["type"] == "http.response.start":
                self.visible.append(await asyncio.create_task(independent_count(), context=Context()))
            await send(message)

        await self.app(scope, receive, observe)


async def test_live_acceptance_is_committed_before_response_start(manual_schedule: AgentScheduleRecord) -> None:
    # Given the actual CRUD request transaction and an independent response observer.
    observed = CommitObserver(schedule_app(manual_schedule))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=observed), base_url="http://qa") as client:
        # When accepting a manual request over HTTP.
        response = await client.post(f"/agents/{manual_schedule.agent_id}/schedules/{manual_schedule.id}/run")
    # Then another connection sees the accepted row before HTTP response headers.
    assert response.status_code == 200
    assert observed.visible == [1]
    assert response.json()["status"] == "triggered"
    assert response.json()["run_id"]


async def test_live_keys_deduplicate_only_within_requester_and_schedule(manual_schedule: AgentScheduleRecord) -> None:
    # Given concurrent retries, an independent requester, and another schedule.
    other_user = uuid4()
    async with connection_ctx() as db:
        await db.execute("INSERT INTO users (id, display_name) VALUES (%(id)s, 'Other')", {"id": other_user})
    other = await agent_schedule_dao.create(
        obj_in={
            "agent_id": manual_schedule.agent_id,
            "name": "Other",
            "instruction": "Other instruction",
            "cron_expr": "0 0 * * *",
            "created_by": manual_schedule.created_by,
        }
    )
    # When accepting keyed retries, scope changes, and unkeyed requests concurrently.
    accepted = await asyncio.gather(
        *(runs.accept(manual_schedule.id, manual_schedule.created_by, "retry") for _ in range(8))
    )
    distinct = await asyncio.gather(
        runs.accept(manual_schedule.id, other_user, "retry"),
        runs.accept(other.id, manual_schedule.created_by, "retry"),
        runs.accept(manual_schedule.id, manual_schedule.created_by, None),
        runs.accept(manual_schedule.id, manual_schedule.created_by, None),
    )
    # Then retries share one receipt, while each independent request has its own identity.
    assert all(run is not None for run in [*accepted, *distinct])
    assert len({run.id for run in accepted if run is not None}) == 1
    assert len({run.id for run in [*accepted, *distinct] if run is not None}) == 5


async def test_live_competing_manual_claim_skips_locked_row(manual_schedule: AgentScheduleRecord) -> None:
    # Given one accepted request held by an uncommitted worker claim.
    accepted = await runs.accept(manual_schedule.id, manual_schedule.created_by, None)
    async with connection_ctx():
        (claim,) = await runs.claim_pending(datetime.now(UTC))
        # When another independent connection attempts recovery.
        competitor = asyncio.create_task(runs.claim_pending(datetime.now(UTC)), context=Context())
        result = await asyncio.wait_for(competitor, timeout=2)
    # Then only one worker owns the accepted request.
    assert accepted is not None
    assert claim.id == accepted.id
    assert result == []


async def test_live_reclaim_preserves_snapshot_and_counts_start_once(manual_schedule: AgentScheduleRecord) -> None:
    # Given a started manual run whose schedule is then edited and disabled.
    first = await accept_claim(manual_schedule)
    assert first.lease_token is not None
    now = datetime.now(UTC)
    assert await runs.start(first.id, first.lease_token, now)
    disabled = await agent_schedule_dao.update(
        db_obj=manual_schedule,
        obj_in={
            "is_enabled": False,
            "instruction": "Future revision",
            "cron_expr": "invalid",
        },
    )
    assert first.lease_expires_at is not None
    # When another worker recovers the expired request and starts it concurrently.
    (reclaimed,) = await runs.claim_pending(first.lease_expires_at)
    assert reclaimed.lease_token is not None
    started = await asyncio.gather(
        *(
            runs.start(reclaimed.id, reclaimed.lease_token, first.lease_expires_at + timedelta(seconds=1))
            for _ in range(2)
        )
    )
    # Then the accepted snapshot and first-start bookkeeping survive ownership rotation.
    assert started == [True, True]
    assert reclaimed.instruction == manual_schedule.instruction
    assert reclaimed.lease_token != first.lease_token
    assert await agent_schedule_dao.get(manual_schedule.id) == replace(
        disabled, last_run_at=now, run_count=manual_schedule.run_count + 1
    )


@pytest.mark.parametrize("operation", ["renew", "start", "finish"])
async def test_live_manual_owner_writes_reject_expiry_rotation_and_delete(
    manual_schedule: AgentScheduleRecord, operation: str
) -> None:
    # Given an expired manual claim.
    claim = await accept_claim(manual_schedule)
    token, expires = claim.lease_token, claim.lease_expires_at
    assert token is not None
    assert expires is not None

    async def write() -> bool:
        match operation:
            case "renew":
                return await runs.renew(claim.id, token, expires)
            case "start":
                return await runs.start(claim.id, token, expires)
            case "finish":
                return await runs.finish(claim, expires, "success")
            case _:
                raise AssertionError(operation)

    # When the old owner writes at exact expiry, after reclaim, and after deletion.
    expired = await write()
    (reclaimed,) = await runs.claim_pending(expires)
    rotated = await write()
    await agent_schedule_dao.delete(id=manual_schedule.id)
    deleted = await runs.finish(reclaimed, expires, "success")
    # Then every stale write is fenced, including the previously current owner after deletion.
    assert [expired, rotated, deleted] == [False, False, False]


async def test_live_terminal_history_rolls_back_and_commits_once(manual_schedule: AgentScheduleRecord) -> None:
    # Given a claimed request and a failure between terminal marker and history insertion.
    claim = await accept_claim(manual_schedule)
    now = datetime.now(UTC)

    async def fail_history() -> None:
        async with connection_ctx():
            assert await runs.finish(claim, now, "success")
            raise RuntimeError("history unavailable")

    with pytest.raises(RuntimeError, match="history unavailable"):
        await fail_history()
    # When finalization is retried in the same transaction as its activity row.
    for _ in range(2):
        async with connection_ctx():
            if await runs.finish(claim, now, "success"):
                await agent_activity_log_dao.create(
                    obj_in={
                        "agent_id": claim.agent_id,
                        "action_type": "schedule_run",
                        "summary": "Finished",
                        "detail_json": {
                            "schedule_id": str(claim.schedule_id),
                            "run_id": str(claim.id),
                            "status": "success",
                        },
                    }
                )
    # Then one terminal row and one history row commit together, with no duplicate on retry.
    finished = await runs.get(claim.id)
    assert finished is not None
    assert finished.outcome == "success"
    history = await agent_activity_log_dao.list_schedule_history(claim.agent_id, claim.schedule_id)
    assert len(history) == 1


async def test_live_concurrent_first_starts_increment_once(manual_schedule: AgentScheduleRecord) -> None:
    # Given an unstarted claimed request.
    claim = await accept_claim(manual_schedule)
    token = claim.lease_token
    assert token is not None
    # When competing starts race on the same ownership token.
    results = await asyncio.gather(*(runs.start(claim.id, token, datetime.now(UTC)) for _ in range(3)))
    # Then all valid starts succeed but only the first increments the counter.
    current = await agent_schedule_dao.get(manual_schedule.id)
    assert results == [True, True, True]
    assert current is not None
    assert current.run_count == manual_schedule.run_count + 1


async def test_live_acceptance_failure_rolls_back_without_http_success(
    manual_schedule: AgentScheduleRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    # Given a transaction-flush failure after the acceptance insert.
    monkeypatch.setattr(schedules, "flush_request_transaction", AsyncMock(side_effect=RuntimeError("commit failed")))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=schedule_app(manual_schedule), raise_app_exceptions=False),
        base_url="http://qa",
    ) as client:
        # When acceptance cannot be committed.
        response = await client.post(f"/agents/{manual_schedule.agent_id}/schedules/{manual_schedule.id}/run")
    # Then no success response or recoverable request escapes the rollback.
    assert response.status_code == 500
    assert await runs.claim_pending(datetime.now(UTC)) == []


@pytest.mark.parametrize("failure", ["history", "flush"])
async def test_live_cancel_failure_rolls_back_state_and_history(
    manual_schedule: AgentScheduleRecord, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    # Given claimed pending work and an actual request transaction with a failing boundary.
    claim = await accept_claim(manual_schedule)
    match failure:
        case "history":
            monkeypatch.setattr(agent_activity_log_dao, "create", AsyncMock(side_effect=RuntimeError("history failed")))
        case "flush":
            monkeypatch.setattr(
                schedules, "flush_request_transaction", AsyncMock(side_effect=RuntimeError("commit failed"))
            )
        case _:
            raise AssertionError(failure)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=schedule_app(manual_schedule), raise_app_exceptions=False),
        base_url="http://qa",
    ) as client:
        # When cancellation cannot commit both terminal state and activity.
        response = await client.post(f"/agents/{claim.agent_id}/schedules/{claim.schedule_id}/runs/{claim.id}/cancel")
    # Then no successful acknowledgement, terminal state, lease clearing or history escapes.
    assert response.status_code == 500
    assert await runs.get(claim.id) == claim
    assert await agent_activity_log_dao.list_schedule_history(claim.agent_id, claim.schedule_id) == []
