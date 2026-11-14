"""Live OOM capture: the docker events line parser, the event recorder, the long-lived reader and the pass correlation."""

import asyncio
import json
import subprocess
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import override
from uuid import UUID, uuid4

import pytest
from anyio import EndOfStream
from anyio.abc import ByteReceiveStream, ByteSendStream, Process

from app.config import get_settings
from app.dao.agent_resource_pressure_episode_dao import OpenEpisode
from app.records.agent import AgentRecord
from app.records.organization_plan import OrganizationPlanId
from app.services import agent_resource_pressure as pressure, agent_resource_pressure_events as events
from app.services.agent_resource_pressure_docker import GuestInspection
from app.services.organization_plans import AmbiguousOrganizationTierError, plan_for

GIB = 1024**3
PRO = OrganizationPlanId.PRO
CONTAINER = "7d31ef1e4551856a03adb629f91726329442e304c140586917223cfc19951215"
AGENT = UUID("ca1546d0-dabc-4f8d-a8a1-d7f92b970bd4")
TIME_NANO = 1794592962998812135
# The line `docker events --format '{{json .}}'` printed on Docker 29.9.3 for a 32 MiB guest of the engine's label.
REAL_LINE = (
    f'{{"Type":"container","Action":"oom","Actor":{{"ID":"{CONTAINER}","Attributes":'
    f'{{"desktop.docker.io/ports.scheme":"v2","image":"alpine:3.22","maraclaw.agent_id":"{AGENT}",'
    f'"name":"mc-oom-qa-main-32112"}}}},"scope":"local","time":1794592962,"timeNano":{TIME_NANO}}}'
)


def _payload(container: str = CONTAINER, agent: str | None = str(AGENT), nano: object = TIME_NANO) -> dict[str, object]:
    attributes: dict[str, object] = {"image": "alpine:3.22"}
    if agent is not None:
        attributes[events.AGENT_LABEL] = agent
    return {
        "Type": "container",
        "Action": "oom",
        "Actor": {"ID": container, "Attributes": attributes},
        "scope": "local",
        "time": 1,
        "timeNano": nano,
    }


def _line(container: str = CONTAINER, agent: str | None = str(AGENT), nano: object = TIME_NANO) -> bytes:
    return json.dumps(_payload(container, agent, nano)).encode()


MALFORMED: dict[str, str] = {
    "empty": "",
    "blank": "   ",
    "not-json": "oom happened",
    "array": "[]",
    "null": "null",
    "no-fields": "{}",
    "other-action": json.dumps(_payload() | {"Action": "die"}),
    "other-type": json.dumps(_payload() | {"Type": "image"}),
    "no-actor": json.dumps({key: value for key, value in _payload().items() if key != "Actor"}),
    "empty-id": json.dumps(_payload(container="")),
    "numeric-id": json.dumps(_payload() | {"Actor": {"ID": 123, "Attributes": {events.AGENT_LABEL: str(AGENT)}}}),
    "no-label": json.dumps(_payload(agent=None)),
    "label-not-a-uuid": json.dumps(_payload(agent="not-a-uuid")),
    "no-time": json.dumps({key: value for key, value in _payload().items() if key != "timeNano"}),
    "string-time": json.dumps(_payload(nano="123")),
    "boolean-time": json.dumps(_payload(nano=True)),
    "zero-time": json.dumps(_payload(nano=0)),
    "negative-time": json.dumps(_payload(nano=-5)),
    "fractional-time": json.dumps(_payload(nano=1.5)),
}


class Clock:
    def __init__(self) -> None:
        self.now: float = 0.0

    def __call__(self) -> float:
        return self.now


class FakeStdout(ByteReceiveStream):
    """Scripted child output: the chunks, then either end of stream (the child exits) or a read that never returns."""

    def __init__(self, process: FakeProcess, chunks: Sequence[bytes], park: bool, probe: Callable[[], int]) -> None:
        self.process: FakeProcess = process
        self.chunks: list[bytes] = list(chunks)
        self.park: bool = park
        self.probe: Callable[[], int] = probe
        self.in_flight_at_read: list[int] = []
        self.drained: asyncio.Event = asyncio.Event()

    @override
    async def receive(self, max_bytes: int = 65536) -> bytes:
        self.in_flight_at_read.append(self.probe())
        if self.chunks:
            return self.chunks.pop(0)
        self.drained.set()
        if self.park:
            await asyncio.Event().wait()
        self.process.exit_by_itself()
        raise EndOfStream

    @override
    async def aclose(self) -> None:
        return None


class FakeProcess(Process):
    def __init__(
        self,
        chunks: Sequence[bytes] = (),
        *,
        park: bool = False,
        honors_terminate: bool = True,
        lived: float = 0.0,
        clock: Clock | None = None,
        probe: Callable[[], int] = lambda: 0,
    ) -> None:
        self.output: FakeStdout = FakeStdout(self, chunks, park, probe)
        self.honors_terminate: bool = honors_terminate
        self.lived: float = lived
        self.clock: Clock | None = clock
        self.signals: list[str] = []
        self.code: int | None = None
        self.exited: asyncio.Event = asyncio.Event()
        self.closed: bool = False

    def finish(self, code: int) -> None:
        if self.code is None:
            self.code = code
            self.exited.set()

    def exit_by_itself(self) -> None:
        if self.clock is not None:
            self.clock.now += self.lived
        self.finish(1)

    @override
    async def wait(self) -> int:
        _ = await self.exited.wait()
        return 0 if self.code is None else self.code

    @override
    def terminate(self) -> None:
        self.signals.append("terminate")
        if self.honors_terminate:
            self.finish(-15)

    @override
    def kill(self) -> None:
        self.signals.append("kill")
        self.finish(-9)

    @override
    def send_signal(self, signal: int) -> None:
        self.signals.append(f"signal {signal}")

    @property
    @override
    def pid(self) -> int:
        return 4242

    @property
    @override
    def returncode(self) -> int | None:
        return self.code

    @property
    @override
    def stdin(self) -> ByteSendStream | None:
        return None

    @property
    @override
    def stdout(self) -> ByteReceiveStream | None:
        return self.output

    @property
    @override
    def stderr(self) -> ByteReceiveStream | None:
        return None

    @override
    async def aclose(self) -> None:
        self.closed = True


class Docker:
    """The CLI seam: the children `docker events` yields in turn, the client lookup and `inspect`."""

    def __init__(self) -> None:
        self.children: list[FakeProcess | Exception] = []
        self.commands: list[tuple[list[str], dict[str, object]]] = []
        self.unavailable_for: int = 0
        self.inspections: dict[str, GuestInspection] | None = {}
        self.inspect_calls: list[list[str]] = []

    async def open_process(self, command: list[str], **options: object) -> Process:
        self.commands.append((command, options))
        child = self.children.pop(0)
        if isinstance(child, Exception):
            raise child
        return child

    def client(self) -> SimpleNamespace | None:
        if self.unavailable_for > 0:
            self.unavailable_for -= 1
            return None
        return SimpleNamespace(docker_cmd=["docker", "--host", "unix:///x.sock"])

    def inspect(self, _client: object, references: list[str], _timeout: float) -> dict[str, GuestInspection] | None:
        self.inspect_calls.append(list(references))
        if self.inspections is None:
            return None
        return {ref: self.inspections[ref] for ref in references if ref in self.inspections}


class Episodes:
    """In-memory stand-in for the episode DAO's OOM insert: idempotent per agent and event key."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.rows: dict[tuple[object, object], dict[str, object]] = {}
        self.in_flight: int = 0

    async def record_oom(self, **fields: object) -> None:
        self.in_flight += 1
        try:
            await asyncio.sleep(0)
            self.calls.append(fields)
            _ = self.rows.setdefault((fields["agent_id"], fields["event_key"]), fields)
        finally:
            self.in_flight -= 1


class Agents:
    def __init__(self) -> None:
        self.by_id: dict[UUID, AgentRecord] = {}
        self.failures: list[Exception] = []

    async def get(self, record_id: UUID) -> AgentRecord | None:
        if self.failures:
            raise self.failures.pop(0)
        return self.by_id.get(record_id)


class Warnings:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def bind(self, **_context: object) -> Warnings:
        return self

    def warning(self, message: str, *args: object) -> None:
        self.messages.append(message.format(*args))


@dataclass
class World:
    docker: Docker
    episodes: Episodes
    agents: Agents
    warnings: Warnings
    clock: Clock
    sleeps: list[float]
    tiers: dict[UUID, OrganizationPlanId | Exception | None] = field(
        default_factory=dict[UUID, OrganizationPlanId | Exception | None]
    )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    state = World(Docker(), Episodes(), Agents(), Warnings(), Clock(), [])

    async def tier_for_tenant(tenant_id: UUID | None) -> OrganizationPlanId | None:
        assert tenant_id is not None
        answer = state.tiers.get(tenant_id, PRO)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def sleep(seconds: float) -> None:
        state.sleeps.append(seconds)
        assert len(state.sleeps) < 50, "the reader is spinning"
        await asyncio.sleep(0)

    monkeypatch.setattr(events, "agent_dao", state.agents)
    monkeypatch.setattr(events, "tier_for_tenant", tier_for_tenant)
    monkeypatch.setattr(events, "agent_resource_pressure_episode_dao", state.episodes)
    monkeypatch.setattr(events, "inspect_guests", state.docker.inspect)
    monkeypatch.setattr(events, "open_process", state.docker.open_process)
    monkeypatch.setattr(events, "sleep", sleep)
    monkeypatch.setattr(events, "monotonic", state.clock)
    monkeypatch.setattr(events.agent_manager, "reacquire_docker_client", state.docker.client)
    monkeypatch.setattr(events, "logger", state.warnings)
    return state


@pytest.fixture
async def run_reader(world: World) -> AsyncIterator[Callable[[], asyncio.Task[None]]]:
    tasks: list[asyncio.Task[None]] = []

    def start() -> asyncio.Task[None]:
        task = asyncio.create_task(events.read_oom_events(events.record_oom_event))
        tasks.append(task)
        return task

    yield start
    for task in tasks:
        _ = task.cancel()
    _ = await asyncio.gather(*tasks, return_exceptions=True)


async def _drained(child: FakeProcess) -> None:
    """Wait until the child has been read to the end: every handler for its lines has finished by then."""
    await asyncio.wait_for(child.output.drained.wait(), 2)


def _agent(world: World, *, system: bool = False, tenant: bool = True, agent_id: UUID = AGENT) -> AgentRecord:
    agent = AgentRecord(
        id=agent_id,
        name="Ops",
        creator_id=uuid4(),
        tenant_id=uuid4() if tenant else None,
        is_system=system,
    )
    world.agents.by_id[agent.id] = agent
    return agent


def _guest(world: World, *, limit: int = 3 * GIB, exit_code: int = 137, restarts: int = 3) -> None:
    world.docker.inspections = {
        CONTAINER: GuestInspection(CONTAINER, str(AGENT), False, limit, 1000, True, None, "", exit_code, restarts)
    }


def _event(nano: int = TIME_NANO) -> events.OomEvent:
    return events.OomEvent(CONTAINER, AGENT, nano)


def test_a_real_docker_29_line_parses_into_the_container_agent_and_event_time():
    event = events.parse_oom_event(REAL_LINE)

    assert event == events.OomEvent(CONTAINER, AGENT, TIME_NANO)
    assert event is not None
    assert event.event_key == f"{CONTAINER}:{TIME_NANO}"
    assert event.occurred_at == datetime(2026, 11, 13, 18, 2, 42, 998812, tzinfo=UTC)


@pytest.mark.parametrize("text", list(MALFORMED.values()), ids=list(MALFORMED))
def test_a_line_that_is_not_a_labelled_oom_event_is_not_an_event(text: str):
    assert events.parse_oom_event(text) is None


async def test_a_kill_of_a_tiered_agents_guest_is_stored_against_the_applied_limit(world: World):
    agent = _agent(world)
    _guest(world, limit=3 * GIB)

    await events.record_oom_event(_event())

    (call,) = world.episodes.calls
    assert call == {
        "tenant_id": agent.tenant_id,
        "agent_id": AGENT,
        "agent_name": "Ops",
        "tier": PRO,
        "limit_value": 3 * GIB,
        "threshold_ratio": get_settings().AGENT_RESOURCE_PRESSURE_THRESHOLD,
        "occurred_at": _event().occurred_at,
        "event_key": f"{CONTAINER}:{TIME_NANO}",
        "detail": {"exit_code": 137, "restart_count": 3, "tier_limit": int(plan_for(PRO).guest_memory_bytes)},
    }
    assert world.docker.inspect_calls == [[CONTAINER]]


async def test_the_same_event_twice_is_one_row(world: World):
    _ = _agent(world)
    _guest(world)

    await events.record_oom_event(_event())
    await events.record_oom_event(_event())

    assert len(world.episodes.calls) == 2
    assert len(world.episodes.rows) == 1


@pytest.mark.parametrize("reason", ["missing", "system", "tenantless", "untiered"])
async def test_a_kill_of_a_guest_that_is_not_tier_governed_is_not_stored(world: World, reason: str):
    if reason != "missing":
        agent = _agent(world, system=reason == "system", tenant=reason != "tenantless")
        if reason == "untiered" and agent.tenant_id is not None:
            world.tiers[agent.tenant_id] = None
    _guest(world)

    await events.record_oom_event(_event())

    assert world.episodes.calls == []
    assert world.docker.inspect_calls == []
    assert world.warnings.messages == []


async def test_an_ambiguous_tier_skips_the_kill_with_a_warning_naming_only_the_agent(world: World):
    agent = _agent(world)
    assert agent.tenant_id is not None
    world.tiers[agent.tenant_id] = AmbiguousOrganizationTierError(agent.tenant_id)

    await events.record_oom_event(_event())

    assert world.episodes.calls == []
    assert world.warnings.messages == [
        f"[ResourcePressure] ambiguous organization tier; agent={AGENT} OOM event skipped"
    ]


@pytest.mark.parametrize("answer", ["docker-down", "container-gone", "no-client"])
async def test_a_kill_is_still_stored_against_the_tier_limit_when_docker_cannot_describe_the_guest(
    world: World, answer: str
):
    _ = _agent(world)
    if answer == "docker-down":
        world.docker.inspections = None
    elif answer == "no-client":
        world.docker.unavailable_for = 1

    await events.record_oom_event(_event())

    (call,) = world.episodes.calls
    tier_limit = int(plan_for(PRO).guest_memory_bytes)
    assert (call["limit_value"], call["detail"]) == (tier_limit, {"tier_limit": tier_limit})


async def test_a_guest_without_a_memory_limit_is_not_recorded(world: World):
    _ = _agent(world)
    _guest(world, limit=0)

    await events.record_oom_event(_event())

    assert world.episodes.calls == []


async def test_the_child_is_started_without_a_shell_stdin_or_stderr_and_filtered_to_oom_events_of_labelled_guests(
    world: World, run_reader: Callable[[], asyncio.Task[None]]
):
    child = FakeProcess(park=True)
    world.docker.children = [child]

    _ = run_reader()
    await _drained(child)

    ((command, options),) = world.docker.commands
    assert command == [
        "docker",
        "--host",
        "unix:///x.sock",
        "events",
        "--filter",
        "event=oom",
        "--filter",
        "label=maraclaw.agent_id",
        "--format",
        "{{json .}}",
    ]
    assert options == {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL}


async def test_one_event_line_becomes_one_row(world: World, run_reader: Callable[[], asyncio.Task[None]]):
    _ = _agent(world)
    _guest(world)
    child = FakeProcess([REAL_LINE.encode() + b"\n"], park=True)
    world.docker.children = [child]

    _ = run_reader()
    await _drained(child)

    assert list(world.episodes.rows) == [(AGENT, f"{CONTAINER}:{TIME_NANO}")]
    assert world.warnings.messages == []


async def test_a_duplicate_line_is_handled_twice_and_stored_once(
    world: World, run_reader: Callable[[], asyncio.Task[None]]
):
    _ = _agent(world)
    _guest(world)
    child = FakeProcess([(_line() + b"\n") * 2], park=True)
    world.docker.children = [child]

    _ = run_reader()
    await _drained(child)

    assert len(world.episodes.rows) == 1


async def test_malformed_lines_are_skipped_without_echoing_them_and_the_next_line_is_still_read(
    world: World, run_reader: Callable[[], asyncio.Task[None]]
):
    _ = _agent(world)
    _guest(world)
    child = FakeProcess([b"garbage secret\n", b"\xff\xfe not utf-8\n", b"{}\n", b"\n  \n", _line() + b"\n"], park=True)
    world.docker.children = [child]

    _ = run_reader()
    await _drained(child)

    assert len(world.episodes.rows) == 1
    assert world.warnings.messages == [
        "[ResourcePressure] skipped a malformed docker event line (14 bytes)",
        "[ResourcePressure] skipped a malformed docker event line (12 bytes)",
        "[ResourcePressure] skipped a malformed docker event line (2 bytes)",
    ]
    assert all("secret" not in message for message in world.warnings.messages)


async def test_lines_are_reassembled_across_chunks_and_a_final_unterminated_line_is_read(
    world: World, run_reader: Callable[[], asyncio.Task[None]]
):
    _ = _agent(world)
    _guest(world)
    first, second, third = _line(nano=1000), _line(nano=2000), _line(nano=3000)
    cut = len(first) // 2
    child = FakeProcess([first[:cut], first[cut:] + b"\n" + second[:7], second[7:] + b"\n" + third], park=True)
    world.docker.children = [child]

    _ = run_reader()
    await _drained(child)

    assert sorted(str(key) for _, key in world.episodes.rows) == [f"{CONTAINER}:1000", f"{CONTAINER}:2000"]


async def test_a_failing_recorder_is_logged_without_details_and_does_not_end_the_stream(
    world: World, run_reader: Callable[[], asyncio.Task[None]]
):
    _ = _agent(world)
    _guest(world)
    world.agents.failures = [OSError("database down at /var/secret/path")]
    child = FakeProcess([_line(nano=1000) + b"\n" + _line(nano=2000) + b"\n"], park=True)
    world.docker.children = [child]

    _ = run_reader()
    await _drained(child)

    assert list(world.episodes.rows) == [(AGENT, f"{CONTAINER}:2000")]
    assert world.warnings.messages == [f"[ResourcePressure] recording an OOM event failed agent={AGENT}"]
    assert world.sleeps == []


async def test_the_reader_holds_no_database_work_while_it_waits_on_the_pipe(
    world: World, run_reader: Callable[[], asyncio.Task[None]]
):
    _ = _agent(world)
    _guest(world)
    child = FakeProcess(
        [_line(nano=1000) + b"\n", _line(nano=2000) + b"\n"], park=True, probe=lambda: world.episodes.in_flight
    )
    world.docker.children = [child]

    _ = run_reader()
    await _drained(child)

    assert child.output.in_flight_at_read == [0, 0, 0]


async def test_the_reader_restarts_the_child_after_it_ends_and_reaps_the_first(
    world: World, run_reader: Callable[[], asyncio.Task[None]]
):
    _ = _agent(world)
    _guest(world)
    first = FakeProcess()
    second = FakeProcess([_line() + b"\n"], park=True)
    world.docker.children = [first, second]

    _ = run_reader()
    await _drained(second)

    assert len(world.docker.commands) == 2
    assert world.sleeps == [events.RESTART_BASE_SECONDS]
    assert (first.code, first.closed) == (1, True)
    assert world.warnings.messages == ["[ResourcePressure] docker events exited code=1; restarting"]
    assert len(world.episodes.rows) == 1


async def test_start_failures_back_off_with_doubling_delays_and_a_long_lived_child_resets_them(
    world: World, run_reader: Callable[[], asyncio.Task[None]]
):
    parked = FakeProcess(park=True)
    world.docker.children = [
        FileNotFoundError("docker"),
        PermissionError(),
        FakeProcess(lived=events.STABLE_SECONDS, clock=world.clock),
        FakeProcess(),
        parked,
    ]

    _ = run_reader()
    await _drained(parked)

    assert world.sleeps == [1.0, 2.0, 1.0, 2.0]
    assert "[ResourcePressure] docker events reader failed" in world.warnings.messages


async def test_docker_being_unavailable_delays_the_child_until_a_client_exists(
    world: World, run_reader: Callable[[], asyncio.Task[None]]
):
    child = FakeProcess(park=True)
    world.docker.children = [child]
    world.docker.unavailable_for = 2

    _ = run_reader()
    await _drained(child)

    assert world.sleeps == [1.0, 2.0]
    assert len(world.docker.commands) == 1
    assert world.warnings.messages == ["[ResourcePressure] Docker unavailable; OOM event reader will retry"] * 2


async def test_cancelling_the_reader_terminates_and_reaps_the_child(
    world: World, run_reader: Callable[[], asyncio.Task[None]]
):
    child = FakeProcess(park=True)
    world.docker.children = [child]
    task = run_reader()
    await _drained(child)

    _ = task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert child.signals == ["terminate"]
    assert (child.code, child.closed) == (-15, True)
    assert world.sleeps == []


async def test_a_child_that_ignores_termination_is_killed_after_the_grace_period_and_still_reaped(
    world: World, run_reader: Callable[[], asyncio.Task[None]], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(events, "STOP_GRACE_SECONDS", 0.01)
    child = FakeProcess(park=True, honors_terminate=False)
    world.docker.children = [child]
    task = run_reader()
    await _drained(child)

    _ = task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert child.signals == ["terminate", "kill"]
    assert (child.code, child.closed) == (-9, True)


@dataclass
class Pass:
    stored: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    queries: list[tuple[UUID, str, datetime]] = field(default_factory=list[tuple[UUID, str, datetime]])
    reader_kill: datetime | None = None
    inspections: dict[str, GuestInspection] = field(default_factory=dict[str, GuestInspection])


class PassEpisodes:
    """The episode DAO as one sampling pass uses it, with the reader's rows reduced to one kill time."""

    def __init__(self, state: Pass) -> None:
        self.state: Pass = state

    async def open_for_agents(self, agent_ids: Sequence[UUID]) -> list[OpenEpisode]:
        return []

    async def close_open(self, *_args: object, **_kwargs: object) -> int:
        return 0

    async def oom_event_recorded_since(self, agent_id: UUID, container_id: str, since: datetime) -> bool:
        self.state.queries.append((agent_id, container_id, since))
        return self.state.reader_kill is not None and self.state.reader_kill >= since

    async def record_oom(self, **fields: object) -> None:
        self.state.stored.append(fields)


@pytest.fixture
def passes(monkeypatch: pytest.MonkeyPatch) -> Pass:
    state = Pass()

    async def tier_for_tenant(_tenant_id: UUID | None) -> OrganizationPlanId | None:
        return PRO

    async def charged_for_agents(_db: object, _agent_ids: Sequence[UUID]) -> dict[UUID, int]:
        return {}

    @asynccontextmanager
    async def connection() -> AsyncGenerator[None]:
        yield

    monkeypatch.setattr(pressure, "agent_resource_pressure_episode_dao", PassEpisodes(state))
    monkeypatch.setattr(pressure, "tier_for_tenant", tier_for_tenant)
    monkeypatch.setattr(
        pressure,
        "inspect_guests",
        lambda _client, references, _timeout: {
            ref: state.inspections[ref] for ref in references if ref in state.inspections
        },
    )
    monkeypatch.setattr(pressure, "guest_stats", lambda _client, _ids, _timeout: {})
    monkeypatch.setattr(pressure, "connection_ctx", connection)
    monkeypatch.setattr(pressure.agent_storage_dao, "charged_for_agents", charged_for_agents)
    monkeypatch.setattr(
        pressure.agent_manager, "reacquire_docker_client", lambda: SimpleNamespace(docker_cmd=["docker"])
    )
    return state


STARTED = datetime(2026, 11, 13, 18, 2, 43, 216441, tzinfo=UTC)
FINISHED_RAW = "2026-11-13T18:02:43.291703135Z"


def _flagged(state: Pass, *, running: bool, started_at: datetime | None) -> AgentRecord:
    agent = AgentRecord(id=uuid4(), name="Ops", creator_id=uuid4(), tenant_id=uuid4(), container_id=uuid4().hex * 2)
    assert agent.container_id is not None
    state.inspections[agent.container_id] = GuestInspection(
        container_id=agent.container_id,
        owner=str(agent.id),
        running=running,
        memory_limit=2 * GIB,
        millicpus_limit=1000,
        oom_killed=True,
        finished_at=None if running else STARTED + timedelta(milliseconds=75),
        finished_at_raw="0001-01-01T00:00:00Z" if running else FINISHED_RAW,
        exit_code=0 if running else 137,
        restart_count=3,
        started_at=started_at,
    )
    return agent


@pytest.mark.parametrize("running", [False, True], ids=["restarting-after-the-kill", "child-killed-while-running"])
async def test_a_pass_stores_nothing_for_a_flag_whose_kill_the_reader_already_stored(passes: Pass, running: bool):
    agent = _flagged(passes, running=running, started_at=STARTED)
    passes.reader_kill = STARTED + timedelta(milliseconds=77)

    await pressure.sample_page([agent])

    assert passes.stored == []
    assert passes.queries == [(agent.id, agent.container_id, STARTED)]


async def test_a_pass_stores_the_flag_when_the_readers_only_row_belongs_to_an_earlier_run(passes: Pass):
    agent = _flagged(passes, running=False, started_at=STARTED)
    passes.reader_kill = STARTED - timedelta(seconds=1)

    await pressure.sample_page([agent])

    (stored,) = passes.stored
    assert stored["event_key"] == f"{agent.container_id}:{FINISHED_RAW}"
    assert stored["agent_id"] == agent.id


async def test_a_pass_stores_the_flag_when_the_reader_stored_nothing(passes: Pass):
    agent = _flagged(passes, running=True, started_at=STARTED)

    await pressure.sample_page([agent])

    (stored,) = passes.stored
    assert stored["event_key"] == f"{agent.container_id}:0001-01-01T00:00:00Z"


async def test_a_pass_without_a_start_time_asks_nothing_and_stores_the_flag_as_before(passes: Pass):
    agent = _flagged(passes, running=False, started_at=None)
    passes.reader_kill = STARTED

    await pressure.sample_page([agent])

    assert passes.queries == []
    assert len(passes.stored) == 1
