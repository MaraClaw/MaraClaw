import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Literal
from unittest.mock import ANY, AsyncMock
from uuid import UUID, uuid4

import pytest

from app.db.session import get_connection
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.openclaw_dispatch import SharedDispatchRequest
from app.services import (
    openclaw_gateway_delivery as delivery,
    openclaw_gateway_rpc as rpc,
    openclaw_routing as routing,
    turn_timing,
)
from app.services.agent_setup_lock import WorkspaceLockTimeoutError
from app.services.shared_chat import shared_session_id
from app.services.turn_timing import AdapterTiming, TurnTimeline


class NativeQueue:
    def __init__(self, shared: bool) -> None:
        self.agent = AgentRecord(id=uuid4(), name="QA", creator_id=uuid4())
        self.row = GatewayMessageRecord(
            id=uuid4(),
            agent_id=self.agent.id,
            content="human",
            conversation_id=str(shared_session_id(self.agent.id)) if shared else "ordinary-thread",
        )
        self.claims: list[GatewayMessageRecord] = []
        self.releases: list[GatewayMessageRecord] = []

    async def get_for_agent(self, message_id: UUID, agent_id: UUID) -> GatewayMessageRecord | None:
        return self.row if (self.row.id, self.row.agent_id) == (message_id, agent_id) else None

    async def ordinary_claim(self, agent: AgentRecord, message_id: UUID) -> GatewayMessageRecord | None:
        assert (agent.id, message_id) == (self.row.agent_id, self.row.id)
        if self.row.status != "pending":
            return None
        self.row = replace(self.row, status="delivered", delivered_at=datetime.now(UTC))
        self.claims.append(self.row)
        return self.row

    async def shared_claim(self, request: SharedDispatchRequest) -> GatewayMessageRecord | None:
        assert request.agent_id == self.agent.id
        return await self.ordinary_claim(self.agent, self.row.id)

    async def release(self, claimed: GatewayMessageRecord) -> GatewayMessageRecord | None:
        self.releases.append(claimed)
        if self.row.status != "delivered" or self.row.delivered_at != claimed.delivered_at:
            return None
        self.row = replace(self.row, status="pending", delivered_at=None)
        return self.row

    async def run(self) -> None:
        await routing._run_inbox_wake(self.agent, self.row.content, self.row.id)


@pytest.fixture(params=[False, True], ids=["ordinary", "shared"])
def queue(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> NativeQueue:
    value = NativeQueue(request.param is True)
    monkeypatch.setattr(routing, "assert_work_allowed", AsyncMock())
    monkeypatch.setattr(routing, "load_agent_for_new_work", AsyncMock(return_value=value.agent))
    monkeypatch.setattr(routing.gateway_message_dao, "get_for_agent", value.get_for_agent)
    monkeypatch.setattr(routing.gateway_message_dao, "release_delivery", value.release)
    monkeypatch.setattr(routing, "claim_ordinary_turn", value.ordinary_claim)
    monkeypatch.setattr(routing, "claim_ready_shared", value.shared_claim)
    monkeypatch.setattr(delivery, "settle_native_report", AsyncMock(return_value=None))
    return value


@pytest.mark.parametrize("admission", ["not_submitted", "rejected"])
async def test_proven_refusal_releases_exact_claim(
    queue: NativeQueue, monkeypatch: pytest.MonkeyPatch, admission: Literal["not_submitted", "rejected"]
) -> None:
    execute = AsyncMock(return_value=rpc.NotAdmittedOutcome(str(queue.row.id), admission, "bootstrap_pending"))
    monkeypatch.setattr(rpc, "execute_gateway_turn", execute)
    await queue.run()
    assert queue.row.status == "pending"
    assert queue.row.delivered_at is None
    assert queue.releases == queue.claims
    execute.assert_awaited_once_with(queue.agent, queue.claims[0], timing=ANY)
    assert execute.await_args is not None
    assert isinstance(execute.await_args.kwargs["timing"], TurnTimeline)


@pytest.mark.parametrize("admission", ["unknown", "accepted"])
async def test_uncertain_admission_is_never_replayed(
    queue: NativeQueue, monkeypatch: pytest.MonkeyPatch, admission: Literal["unknown", "accepted"]
) -> None:
    execute = AsyncMock(
        return_value=rpc.UncertainOutcome(str(queue.row.id), admission, str(queue.row.id), "disconnect")
    )
    monkeypatch.setattr(rpc, "execute_gateway_turn", execute)
    await queue.run()
    await queue.run()
    assert queue.row.status == "delivered"
    assert queue.releases == []
    assert execute.await_count == 1


@pytest.mark.parametrize("failure", [RuntimeError("private transport failure"), asyncio.CancelledError()])
async def test_interruption_retains_delivery(
    queue: NativeQueue, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    monkeypatch.setattr(rpc, "execute_gateway_turn", AsyncMock(side_effect=failure))
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await queue.run()
    else:
        await queue.run()
    assert queue.row.status == "delivered"
    assert queue.releases == []


@pytest.mark.parametrize("newer", ["completed", "new-claim"])
async def test_refusal_release_cannot_overwrite_newer_state(
    queue: NativeQueue, monkeypatch: pytest.MonkeyPatch, newer: str
) -> None:
    async def execute(
        _agent: AgentRecord, claimed: GatewayMessageRecord, *, timing: TurnTimeline | None = None
    ) -> rpc.NativeTurnOutcome:
        assert claimed.delivered_at is not None
        queue.row = (
            replace(queue.row, status="completed")
            if newer == "completed"
            else replace(queue.row, delivered_at=claimed.delivered_at + timedelta(seconds=1))
        )
        return rpc.NotAdmittedOutcome(str(claimed.id), "rejected", "rejected")

    monkeypatch.setattr(rpc, "execute_gateway_turn", execute)
    await queue.run()
    assert queue.releases == queue.claims
    assert queue.row.status == ("completed" if newer == "completed" else "delivered")
    if newer != "completed":
        assert queue.row.delivered_at != queue.releases[0].delivered_at


@pytest.mark.parametrize(
    ("status", "text", "expected"),
    [
        ("ok", "answer", "answer"),
        ("ok", "", "The agent completed the request without a text response."),
        ("error", "SECRET token", "The agent could not complete this request."),
        ("timeout", "SECRET path", "The agent timed out before completing this request."),
    ],
)
async def test_terminal_outcomes_settle_truthful_safe_result(
    queue: NativeQueue,
    monkeypatch: pytest.MonkeyPatch,
    status: Literal["ok", "error", "timeout"],
    text: str,
    expected: str,
) -> None:
    async def execute(
        _agent: AgentRecord, claimed: GatewayMessageRecord, *, timing: TurnTimeline | None = None
    ) -> rpc.NativeTurnOutcome:
        assert get_connection() is None
        return rpc.TerminalOutcome(str(claimed.id), str(claimed.id), status, text, "private-code")

    settle = AsyncMock(return_value=delivery.RecordedReport(queue.row, None))
    publish = AsyncMock()
    monkeypatch.setattr(rpc, "execute_gateway_turn", execute)
    monkeypatch.setattr(delivery, "settle_native_report", settle)
    monkeypatch.setattr(delivery, "publish_report", publish)
    await queue.run()
    settle.assert_awaited_once_with(queue.agent, queue.row.id, expected)
    publish.assert_awaited_once_with(queue.agent, settle.return_value)
    assert queue.releases == []


async def test_repeated_cancellation_joins_exact_refusal_release(
    queue: NativeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, finish = asyncio.Event(), asyncio.Event()
    release = queue.release

    async def delayed(claimed: GatewayMessageRecord) -> GatewayMessageRecord | None:
        started.set()
        await finish.wait()
        return await release(claimed)

    monkeypatch.setattr(routing.gateway_message_dao, "release_delivery", delayed)
    monkeypatch.setattr(
        rpc,
        "execute_gateway_turn",
        AsyncMock(return_value=rpc.NotAdmittedOutcome(str(queue.row.id), "rejected", "rejected")),
    )
    task = asyncio.create_task(queue.run())
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert queue.row.status == "pending"
    assert queue.releases == queue.claims


@pytest.fixture
def timing_lines(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str | int]]:
    captured: list[dict[str, str | int]] = []
    monkeypatch.setattr(
        turn_timing, "logger", SimpleNamespace(info=lambda _t, payload: captured.append(json.loads(payload)))
    )
    return captured


async def test_settled_turn_emits_one_timeline_with_every_mark(
    queue: NativeQueue, monkeypatch: pytest.MonkeyPatch, timing_lines: list[dict[str, str | int]]
) -> None:
    queue.row.created_at = datetime.now(UTC) - timedelta(seconds=2)

    async def execute(
        _agent: AgentRecord, claimed: GatewayMessageRecord, *, timing: TurnTimeline | None = None
    ) -> rpc.NativeTurnOutcome:
        assert timing is not None
        timing.mark("exec_started")
        return rpc.TerminalOutcome(str(claimed.id), str(claimed.id), "ok", "answer", None, AdapterTiming(5, 20, 900))

    monkeypatch.setattr(rpc, "execute_gateway_turn", execute)
    monkeypatch.setattr(
        delivery, "settle_native_report", AsyncMock(return_value=delivery.RecordedReport(queue.row, None))
    )
    monkeypatch.setattr(delivery, "publish_report", AsyncMock())
    await queue.run()
    (line,) = timing_lines
    assert line["outcome"] == "terminal"
    assert line["message_id"] == str(queue.row.id)
    assert list(line) == [
        "message_id",
        "agent_id",
        "outcome",
        "claimed",
        "exec_started",
        "rpc_returned",
        "settled",
        "published",
        "adapter_hello_ok",
        "adapter_accepted",
        "adapter_terminal",
    ]
    marks = {key: value for key, value in line.items() if isinstance(value, int)}
    assert (marks["adapter_hello_ok"], marks["adapter_accepted"], marks["adapter_terminal"]) == (5, 20, 900)
    assert (
        2000
        <= marks["claimed"]
        <= marks["exec_started"]
        <= marks["rpc_returned"]
        <= marks["settled"]
        <= marks["published"]
    )


@pytest.mark.parametrize(
    ("kind", "result"),
    [
        ("not_admitted", rpc.NotAdmittedOutcome("m", "rejected", "rejected")),
        ("uncertain", rpc.UncertainOutcome("m", "unknown", None, "disconnect")),
        ("released", WorkspaceLockTimeoutError()),
        ("failed", RuntimeError("private transport failure")),
    ],
)
async def test_unsettled_turns_emit_one_timeline_with_their_outcome(
    queue: NativeQueue,
    monkeypatch: pytest.MonkeyPatch,
    timing_lines: list[dict[str, str | int]],
    kind: str,
    result: rpc.NativeTurnOutcome | Exception,
) -> None:
    execute = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
    monkeypatch.setattr(rpc, "execute_gateway_turn", execute)
    await queue.run()
    (line,) = timing_lines
    assert line["outcome"] == kind
    expected = {"message_id", "agent_id", "outcome", "claimed"}
    if not isinstance(result, Exception):
        expected.add("rpc_returned")
    assert set(line) == expected


async def test_unclaimed_wake_emits_no_timeline(
    queue: NativeQueue, monkeypatch: pytest.MonkeyPatch, timing_lines: list[dict[str, str | int]]
) -> None:
    queue.row = replace(queue.row, status="completed")
    monkeypatch.setattr(rpc, "execute_gateway_turn", AsyncMock())
    await queue.run()
    assert timing_lines == []
