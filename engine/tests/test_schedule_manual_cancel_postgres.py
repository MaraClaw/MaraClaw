from datetime import UTC, datetime
from typing import final

import anyio
import httpx
import pytest
from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.dao.activity_log_dao import agent_activity_log_dao
from app.dao.schedule_dao import agent_schedule_dao
from app.dao.schedule_manual_run_dao import schedule_manual_run_dao as runs
from app.db.session import connection_ctx
from app.records.schedule import AgentScheduleRecord
from app.services.schedule_lease import in_fresh_schedule_context
from app.services.schedule_manual_control import cancel_manual_run
from test_schedule_occurrence_postgres import (
    accept_claim,
    live_schedule,
    manual_schedule,
    schedule_app,
)

__all__ = ["live_schedule", "manual_schedule"]


@final
class CancellationCommitObserver:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.visible: list[tuple[int, int]] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def committed() -> tuple[int, int]:
            async with connection_ctx() as db:
                row = await db.fetchone(
                    "SELECT (SELECT count(*) FROM agent_schedule_manual_runs WHERE outcome = 'cancelled' "
                    "AND finished_at IS NOT NULL AND lease_token IS NULL AND lease_expires_at IS NULL) AS runs, "
                    "(SELECT count(*) FROM agent_activity_logs WHERE action_type = 'schedule_run') AS history"
                )
                assert row is not None
                return int(str(row["runs"])), int(str(row["history"]))

        async def observe(message: Message) -> None:
            if message["type"] == "http.response.start":
                self.visible.append(await in_fresh_schedule_context(committed()))
            await send(message)

        await self.app(scope, receive, observe)


async def test_live_cancel_commits_run_and_history_before_response_start(manual_schedule: AgentScheduleRecord) -> None:
    # Given a claimed run and an independent connection observing response headers.
    run = await accept_claim(manual_schedule)
    observed = CancellationCommitObserver(schedule_app(manual_schedule))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=observed), base_url="http://qa") as client:
        # When cancellation returns its HTTP receipt.
        response = await client.post(f"/agents/{run.agent_id}/schedules/{run.schedule_id}/runs/{run.id}/cancel")
    # Then terminal state, revoked leases and its history are already externally visible.
    assert response.status_code == 200
    assert observed.visible == [(1, 1)]
    assert response.json()["status"] == "cancelled"
    assert response.json()["can_cancel"] is False
    history = await agent_activity_log_dao.list_schedule_history(run.agent_id, run.schedule_id)
    assert len(history) == 1
    assert history[0].detail_json == {
        "schedule_id": str(run.schedule_id),
        "run_id": str(run.id),
        "instruction": run.instruction,
        "status": "cancelled",
        "reason": "user_cancelled",
    }


async def test_live_concurrent_and_repeated_cancel_record_one_history(manual_schedule: AgentScheduleRecord) -> None:
    # Given a pending claimed receipt and independent competing HTTP requests.
    run = await accept_claim(manual_schedule)
    responses: list[httpx.Response] = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=schedule_app(manual_schedule)), base_url="http://qa"
    ) as client:
        path = f"/agents/{run.agent_id}/schedules/{run.schedule_id}/runs/{run.id}/cancel"

        async def cancel() -> None:
            responses.append(await in_fresh_schedule_context(client.post(path)))

        # When simultaneous cancellations and a later retry target the same run.
        async with anyio.create_task_group() as tasks:
            for _ in range(3):
                tasks.start_soon(cancel)
        responses.append(await client.post(path))
    # Then every caller sees the same committed terminal receipt and only one activity exists.
    assert [response.status_code for response in responses] == [200] * 4
    assert all(response.json() == responses[0].json() for response in responses)
    assert len(await agent_activity_log_dao.list_schedule_history(run.agent_id, run.schedule_id)) == 1
    assert await agent_schedule_dao.get(manual_schedule.id) == manual_schedule


@pytest.mark.parametrize("winner", ["start", "cancel"])
async def test_live_start_cancel_lock_winner_is_authoritative(
    manual_schedule: AgentScheduleRecord, winner: str
) -> None:
    # Given a first writer holding the row lock and a separate PostgreSQL transaction.
    claim = await accept_claim(manual_schedule)
    token = claim.lease_token
    assert token is not None
    entered = anyio.Event()
    pids: list[int] = []
    result: list[bool | int] = []

    async def compete() -> None:
        async def transaction() -> None:
            async with connection_ctx() as db:
                pids.append(db.raw.info.backend_pid)
                entered.set()
                if winner == "cancel":
                    result.append(await runs.start(claim.id, token, datetime.now(UTC)))
                else:
                    try:
                        await cancel_manual_run(claim)
                    except HTTPException as error:
                        result.append(error.status_code)
                    else:
                        result.append(200)

        await in_fresh_schedule_context(transaction())

    # When the second operation really blocks on the first operation's uncommitted row.
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tasks, connection_ctx() as db:
            if winner == "start":
                assert await runs.start(claim.id, token, datetime.now(UTC))
            else:
                await cancel_manual_run(claim)
            tasks.start_soon(compete)
            await entered.wait()
            while True:
                blocked = await db.fetchone(
                    "SELECT cardinality(pg_blocking_pids(%(pid)s)) > 0 AS blocked", {"pid": pids[0]}
                )
                assert blocked is not None
                if blocked["blocked"]:
                    break
    # Then PostgreSQL predicate rechecking rejects the losing state transition.
    assert result == ([409] if winner == "start" else [False])
    current = await runs.get(claim.id)
    schedule = await agent_schedule_dao.get(manual_schedule.id)
    assert current is not None
    assert schedule is not None
    assert (current.started_at is not None) is (winner == "start")
    assert (current.finished_at is not None) is (winner == "cancel")
    assert schedule.run_count == manual_schedule.run_count + (winner == "start")
    assert len(await agent_activity_log_dao.list_schedule_history(claim.agent_id, claim.schedule_id)) == (
        winner == "cancel"
    )


@pytest.mark.parametrize("state", ["running", "success", "failure", "unknown", "cancelled"])
async def test_live_cancel_conflicts_except_already_cancelled(manual_schedule: AgentScheduleRecord, state: str) -> None:
    # Given work that already started, optionally with a terminal outcome.
    claim = await accept_claim(manual_schedule)
    token = claim.lease_token
    assert token is not None
    assert await runs.start(claim.id, token, datetime.now(UTC))
    if state != "running":
        assert await runs.finish(claim, datetime.now(UTC), state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=schedule_app(manual_schedule)), base_url="http://qa"
    ) as client:
        path = f"/agents/{claim.agent_id}/schedules/{claim.schedule_id}/runs/{claim.id}"
        # When reading then attempting a pending-only cancellation.
        receipt = await client.get(path)
        response = await client.post(path + "/cancel")
    # Then no started or other terminal work is changed or given duplicate history.
    assert receipt.status_code == 200
    assert receipt.json()["status"] == state
    assert receipt.json()["can_cancel"] is False
    assert response.status_code == (200 if state == "cancelled" else 409)
    assert await agent_activity_log_dao.list_schedule_history(claim.agent_id, claim.schedule_id) == []


async def test_live_cancel_after_scoped_read_and_delete_is_not_found(manual_schedule: AgentScheduleRecord) -> None:
    # Given a trusted scoped row whose schedule is subsequently deleted.
    claim = await accept_claim(manual_schedule)
    await agent_schedule_dao.delete(record_id=manual_schedule.id)
    # When cancellation tries to update the formerly authorized identity.
    with pytest.raises(HTTPException) as caught:
        await cancel_manual_run(claim)
    # Then deletion is not misreported as cancellation or a lifecycle conflict.
    assert caught.value.status_code == 404
