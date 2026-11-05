import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.db.session import get_connection
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services import openclaw_dispatch_reconciler as module


@dataclass(slots=True)
class Pacing:
    now: float = 0.0
    entered: asyncio.Queue[float] = field(default_factory=asyncio.Queue)
    go: asyncio.Queue[None] = field(default_factory=asyncio.Queue)

    def clock(self) -> float:
        return self.now

    async def wait(self, seconds: float) -> None:
        self.entered.put_nowait(seconds)
        await self.go.get()


@pytest.fixture
def pacing(monkeypatch: pytest.MonkeyPatch) -> Pacing:
    value = Pacing()
    monkeypatch.setattr(module.anyio, "current_time", value.clock)
    monkeypatch.setattr(module.anyio, "sleep", value.wait)
    return value


async def test_recovery_advances_bounded_keyset_pages_then_wraps(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4())
    first = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="one")
    second = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="two")
    cursor = (datetime(2026, 1, 1, tzinfo=UTC), first.id)
    page = AsyncMock(side_effect=[([first], cursor), ([second], None), ([first], cursor)])
    monkeypatch.setattr(module.gateway_message_dao, "pending_dispatch_page", page)
    monkeypatch.setattr(module.agent_dao, "get_many", AsyncMock(return_value=[agent]))
    ambient: list[object] = []

    def wake(_agent: AgentRecord, _id: object) -> None:
        ambient.append(get_connection())

    wake_call = Mock(side_effect=wake)
    monkeypatch.setattr(module, "wake_shared_lane", wake_call)
    scanner = module.OpenClawDispatchReconciler()
    assert [await scanner.scan_once() for _ in range(3)] == [1, 1, 1]
    assert [call.args[0] for call in page.await_args_list] == [50, 50, 50]
    assert [call.kwargs["after"] for call in page.await_args_list] == [None, cursor, None]
    assert [call.args[1] for call in wake_call.call_args_list] == [first.id, second.id, first.id]
    assert ambient == [None, None, None]


async def test_deleted_agent_is_skipped_without_blocking_other_heads(monkeypatch: pytest.MonkeyPatch) -> None:
    kept = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4())
    missing = GatewayMessageRecord(id=uuid4(), agent_id=uuid4(), content="gone")
    available = GatewayMessageRecord(id=uuid4(), agent_id=kept.id, content="available")
    monkeypatch.setattr(
        module.gateway_message_dao, "pending_dispatch_page", AsyncMock(return_value=([missing, available], None))
    )
    monkeypatch.setattr(module.agent_dao, "get_many", AsyncMock(return_value=[kept]))
    wake = Mock()
    monkeypatch.setattr(module, "wake_shared_lane", wake)
    assert await module.OpenClawDispatchReconciler().scan_once() == 1
    wake.assert_called_once_with(kept, available.id)


async def test_scan_failure_logs_fixed_code_and_continues(pacing: Pacing, monkeypatch: pytest.MonkeyPatch) -> None:
    page = AsyncMock(side_effect=[RuntimeError("SECRET peer and body"), ([], None)])
    monkeypatch.setattr(module.gateway_message_dao, "pending_dispatch_page", page)
    monkeypatch.setattr(module.agent_dao, "get_many", AsyncMock(return_value=[]))
    warning = Mock()
    monkeypatch.setattr(module.logger, "warning", warning)
    task = asyncio.create_task(module.OpenClawDispatchReconciler().run())
    try:
        assert await asyncio.wait_for(pacing.entered.get(), 1) == 1.0
        pacing.go.put_nowait(None)
        assert await asyncio.wait_for(pacing.entered.get(), 1) == 1.0
        assert page.await_count == 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    warning.assert_called_once_with("[OpenClaw] dispatch recovery scan failed code=scan_failed")


async def test_scan_duration_counts_toward_one_second_interval(pacing: Pacing, monkeypatch: pytest.MonkeyPatch) -> None:
    async def page(*_args: object, **_kwargs: object) -> tuple[list[GatewayMessageRecord], None]:
        pacing.now += 0.25
        return [], None

    monkeypatch.setattr(module.gateway_message_dao, "pending_dispatch_page", page)
    monkeypatch.setattr(module.agent_dao, "get_many", AsyncMock(return_value=[]))
    task = asyncio.create_task(module.OpenClawDispatchReconciler().run())
    try:
        assert await asyncio.wait_for(pacing.entered.get(), 1) == 0.75
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("phase", ["scan", "wait"])
async def test_recovery_cancellation_propagates_without_logging(
    pacing: Pacing, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    entered = asyncio.Event()

    async def page(*_args: object, **_kwargs: object) -> tuple[list[GatewayMessageRecord], None]:
        entered.set()
        if phase == "scan":
            await asyncio.Event().wait()
        return [], None

    monkeypatch.setattr(module.gateway_message_dao, "pending_dispatch_page", page)
    monkeypatch.setattr(module.agent_dao, "get_many", AsyncMock(return_value=[]))
    warning = Mock()
    monkeypatch.setattr(module.logger, "warning", warning)
    task = asyncio.create_task(module.OpenClawDispatchReconciler().run())
    try:
        if phase == "scan":
            await asyncio.wait_for(entered.wait(), 1)
        else:
            _ = await asyncio.wait_for(pacing.entered.get(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    warning.assert_not_called()
