import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.dao.personal_burst_dispatch_dao import DueHeadPage
from app.services import personal_burst_reconciler as module
from app.services.personal_burst_reconciler import PersonalBurstReconciler, ReconcilerConfig, ReconcilerPorts

SECRET = "peer-15550001@s.whatsapp.net body-text"


@dataclass(slots=True)
class Pacing:
    """Event-driven pacing: the loop advances only when the test releases a wait; time is a number."""

    now: float = 0.0
    entered: asyncio.Queue[float] = field(default_factory=asyncio.Queue)
    go: asyncio.Queue[None] = field(default_factory=asyncio.Queue)

    def clock(self) -> float:
        return self.now

    async def wait(self, seconds: float) -> None:
        self.entered.put_nowait(seconds)
        await self.go.get()


def scanner(pacing: Pacing, wakes: list[UUID] | None = None) -> PersonalBurstReconciler:
    def wake(_agent: object, message_id: UUID) -> None:
        if wakes is not None:
            wakes.append(message_id)

    return PersonalBurstReconciler(ports=ReconcilerPorts(pacing.wait, pacing.clock, wake))


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    seen: list[tuple[object, ...]] = []
    monkeypatch.setattr(module.logger, "warning", lambda *args, **_kwargs: seen.append(args))
    return seen


async def test_scan_failure_logs_only_a_fixed_code_and_the_loop_continues(monkeypatch, warnings):
    # Given a first scan that fails with sensitive text in the exception, then a scan that succeeds.
    pacing = Pacing()
    outcomes: list[BaseException | DueHeadPage] = [RuntimeError(SECRET), DueHeadPage((), None)]
    scans: list[int] = []

    async def page(*_args: object, **_kwargs: object) -> DueHeadPage:
        scans.append(len(scans))
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def no_agents(_ids: object) -> list[object]:
        return []

    monkeypatch.setattr(module.personal_burst_dispatch_dao, "due_heads_page", page)
    monkeypatch.setattr(module.agent_dao, "get_many", no_agents)
    task = asyncio.create_task(scanner(pacing).run())
    # When the loop paces itself through two passes, Then it survives the failure and scans again.
    _ = await asyncio.wait_for(pacing.entered.get(), 5)
    pacing.go.put_nowait(None)
    _ = await asyncio.wait_for(pacing.entered.get(), 5)
    assert len(scans) == 2
    await stop(task)
    # And the one warning carries a static code, never the exception text, peer or body.
    assert warnings == [("[personal_burst] reconcile failed code={}", "scan_failed")]
    assert SECRET not in repr(warnings)


async def test_wait_is_what_remains_of_the_interval_after_the_scan(monkeypatch):
    pacing = Pacing()

    async def page(*_args: object, **_kwargs: object) -> DueHeadPage:
        pacing.now += 0.25
        return DueHeadPage((), None)

    async def no_agents(_ids: object) -> list[object]:
        return []

    monkeypatch.setattr(module.personal_burst_dispatch_dao, "due_heads_page", page)
    monkeypatch.setattr(module.agent_dao, "get_many", no_agents)
    task = asyncio.create_task(scanner(pacing).run())
    assert await asyncio.wait_for(pacing.entered.get(), 5) == 0.75
    await stop(task)


@pytest.mark.parametrize("phase", ["scan", "wait"])
async def test_cancellation_is_never_swallowed_or_logged(monkeypatch, warnings, phase):
    pacing = Pacing()
    entered = asyncio.Event()

    async def page(*_args: object, **_kwargs: object) -> DueHeadPage:
        entered.set()
        await asyncio.Event().wait()
        return DueHeadPage((), None)

    async def idle(*_args: object, **_kwargs: object) -> DueHeadPage:
        return DueHeadPage((), None)

    async def no_agents(_ids: object) -> list[object]:
        return []

    monkeypatch.setattr(module.personal_burst_dispatch_dao, "due_heads_page", page if phase == "scan" else idle)
    monkeypatch.setattr(module.agent_dao, "get_many", no_agents)
    task = asyncio.create_task(scanner(pacing).run())
    if phase == "scan":
        await asyncio.wait_for(entered.wait(), 5)
    else:
        _ = await asyncio.wait_for(pacing.entered.get(), 5)
    await stop(task)
    assert warnings == []


async def test_deleted_agent_is_skipped_and_others_are_still_woken(monkeypatch):
    gone, kept = uuid4(), uuid4()
    gateway_gone, gateway_kept = uuid4(), uuid4()

    def head(gateway_id: UUID, agent_id: UUID) -> SimpleNamespace:
        return SimpleNamespace(gateway_id=gateway_id, batch=SimpleNamespace(scope=SimpleNamespace(agent_id=agent_id)))

    async def page(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(heads=(head(gateway_gone, gone), head(gateway_kept, kept)), next_after=None)

    async def agents(ids: list[UUID]) -> list[SimpleNamespace]:
        assert set(ids) == {gone, kept}
        return [SimpleNamespace(id=kept)]

    monkeypatch.setattr(module.personal_burst_dispatch_dao, "due_heads_page", page)
    monkeypatch.setattr(module.agent_dao, "get_many", agents)
    wakes: list[UUID] = []
    assert await scanner(Pacing(), wakes).scan_once() == 1
    assert wakes == [gateway_kept]


@pytest.mark.parametrize(
    ("interval", "page_size"), [(0.0, 5), (-1.0, 5), (1.5, 5), (1.0, 0)], ids=["zero", "negative", "slow", "no-page"]
)
def test_config_rejects_loops_slower_than_a_second_or_empty_pages(interval, page_size):
    with pytest.raises(ValueError, match="personal burst reconcile"):
        _ = ReconcilerConfig(interval_seconds=interval, page_size=page_size)
