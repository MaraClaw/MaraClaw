import asyncio
import errno
import json
import os
from contextlib import asynccontextmanager
from threading import Event, current_thread
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pywhales import ClientNotFoundError
from pywhales.client_config import ParsingError
from pywhales.exceptions import DockerException, ObservationTimeout

from app.config import get_settings
from app.dao.user_dao import user_dao
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.user import UserRecord
from app.services import onboarding_greeting, openclaw_gateway_config, openclaw_gateway_rpc as rpc
from app.services.agent_manager import agent_manager
from app.services.guest_identity import GuestIdentity, GuestOwnershipError, GuestResolutionCache
from app.services.openclaw_native_capacity import native_capacity, shutdown_native_capacity
from app.services.openclaw_update_gate import OpenClawUpdateInProgressError
from app.services.storage import LocalStorageBackend
from app.services.turn_timing import AdapterTiming, TurnTimeline


@pytest.fixture(autouse=True)
async def close_native_threads():
    yield
    shutdown_native_capacity()


def running_client():
    return SimpleNamespace(
        container=SimpleNamespace(inspect=lambda _id: SimpleNamespace(state=SimpleNamespace(running=True)))
    )


def test_invalid_or_mismatched_wire_outcome_is_uncertain():
    message_id = str(uuid4())
    terminal = {
        "version": 1,
        "messageId": message_id,
        "kind": "terminal",
        "admission": "accepted",
        "runId": message_id,
        "status": "ok",
        "text": "answer",
        "code": None,
    }
    assert rpc._parse_outcome(json.dumps(terminal), message_id) == rpc.TerminalOutcome(
        message_id, message_id, "ok", "answer", None
    )
    terminal["runId"] = str(uuid4())
    assert rpc._parse_outcome(json.dumps(terminal), message_id) == rpc.UncertainOutcome(
        message_id, "unknown", None, "run_mismatch"
    )
    terminal["unexpected"] = True
    assert rpc._parse_outcome(json.dumps(terminal), message_id) == rpc.UncertainOutcome(
        message_id, "unknown", None, "invalid_output"
    )


def wire_terminal(message_id: str, **extra: object) -> str:
    return json.dumps(
        {
            "version": 1,
            "messageId": message_id,
            "kind": "terminal",
            "admission": "accepted",
            "runId": message_id,
            "status": "ok",
            "text": "answer",
            "code": None,
            **extra,
        }
    )


def test_wire_timing_is_carried_on_every_outcome_kind_and_is_optional():
    message_id = str(uuid4())
    timing = {"helloOk": 12, "accepted": 40, "terminal": 900}
    expected = AdapterTiming(12, 40, 900)
    assert rpc._parse_outcome(wire_terminal(message_id, timing=timing), message_id) == rpc.TerminalOutcome(
        message_id, message_id, "ok", "answer", None, expected
    )
    partial = rpc._parse_outcome(wire_terminal(message_id, timing={"helloOk": 3}), message_id)
    assert partial == rpc.TerminalOutcome(message_id, message_id, "ok", "answer", None, AdapterTiming(hello_ok=3))
    refused = {"version": 1, "messageId": message_id, "kind": "not_admitted", "admission": "rejected", "code": "x"}
    assert rpc._parse_outcome(json.dumps({**refused, "timing": {}}), message_id) == rpc.NotAdmittedOutcome(
        message_id, "rejected", "x", AdapterTiming()
    )
    uncertain = {"version": 1, "messageId": message_id, "kind": "uncertain", "admission": "accepted", "runId": None}
    assert rpc._parse_outcome(
        json.dumps({**uncertain, "code": "c", "timing": {"helloOk": 1, "accepted": 2}}), message_id
    ) == rpc.UncertainOutcome(message_id, "accepted", None, "c", AdapterTiming(1, 2, None))


@pytest.mark.parametrize(
    "timing",
    [{"helloOk": -1}, {"accepted": 1.5}, {"terminal": "9"}, {"helloOk": True}, {"surprise": 1}, [1], "late"],
)
def test_invalid_timing_is_dropped_without_reclassifying_a_valid_outcome(timing):
    message_id = str(uuid4())
    outcome = rpc._parse_outcome(wire_terminal(message_id, timing=timing), message_id)
    assert outcome == rpc.TerminalOutcome(message_id, message_id, "ok", "answer", None)


def test_unknown_top_level_keys_stay_forbidden_next_to_timing():
    message_id = str(uuid4())
    assert rpc._parse_outcome(wire_terminal(message_id, timing={"helloOk": 1}, extra=1), message_id) == (
        rpc.UncertainOutcome(message_id, "unknown", None, "invalid_output")
    )


@pytest.mark.asyncio
async def test_exec_started_is_marked_just_before_the_exec_thread_and_adapter_timing_is_returned(monkeypatch, tmp_path):
    agent = AgentRecord(id=uuid4(), name="test", creator_id=uuid4(), tenant_id=uuid4())
    message = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="hello")
    timeline = TurnTimeline(message.id, agent.id, None)
    seen_at_exec: list[bool] = []

    @asynccontextmanager
    async def exclusion(_agent_id):
        yield

    async def allowed(_agent_id):
        return None

    async def fresh(_agent_id):
        return agent

    def execute(*_args):
        seen_at_exec.append("exec_started" in timeline.marks)
        return wire_terminal(str(message.id), timing={"helloOk": 8, "accepted": 30, "terminal": 700})

    monkeypatch.setattr(rpc, "guest_write_section", exclusion)
    monkeypatch.setattr(rpc, "assert_work_allowed", allowed)
    monkeypatch.setattr(rpc.agent_dao, "get", fresh)
    monkeypatch.setattr(rpc, "resolve_guest", lambda *_: SimpleNamespace(id="owned"))
    monkeypatch.setattr(rpc, "_execute", execute)
    monkeypatch.setattr(agent_manager, "docker_client", running_client())
    monkeypatch.setattr(agent_manager, "_agent_dir", lambda _agent_id: tmp_path)
    monkeypatch.setattr(openclaw_gateway_config, "prepare_native_gateway_files", lambda _directory: None)
    outcome = await rpc.execute_gateway_turn(agent, message, timing=timeline)
    assert isinstance(outcome, rpc.TerminalOutcome)
    assert outcome.timing == AdapterTiming(8, 30, 700)
    assert seen_at_exec == [True]


@pytest.mark.parametrize("admission", ["accepted", "unknown"])
def test_uncertain_wire_outcome_with_wrong_run_is_not_trusted(admission):
    message_id = str(uuid4())
    outcome = {
        "version": 1,
        "messageId": message_id,
        "kind": "uncertain",
        "admission": admission,
        "runId": str(uuid4()),
        "code": "disconnected",
    }
    assert rpc._parse_outcome(json.dumps(outcome), message_id) == rpc.UncertainOutcome(
        message_id, "unknown", None, "run_mismatch"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "stderr", "expected"),
    [
        (75, "bootstrap-agent-container: bootstrap pending: event missing\n", "bootstrap_pending"),
        (75, "other error", "exec_failed"),
        (1, "bootstrap-agent-container: bootstrap pending: event missing\n", "exec_failed"),
    ],
)
async def test_only_exact_bootstrap_refusal_is_not_admitted(monkeypatch, tmp_path, exit_code, stderr, expected):
    agent = AgentRecord(id=uuid4(), name="test", creator_id=uuid4(), tenant_id=uuid4())
    message = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="hello")

    @asynccontextmanager
    async def exclusion(_agent_id):
        yield

    async def allowed(_agent_id):
        return None

    async def fresh(_agent_id):
        return agent

    def execute(*_args):
        raise DockerException(["docker", "exec"], exit_code, stderr=stderr.encode())

    monkeypatch.setattr(rpc, "guest_write_section", exclusion)
    monkeypatch.setattr(rpc, "assert_work_allowed", allowed)
    monkeypatch.setattr(rpc.agent_dao, "get", fresh)
    monkeypatch.setattr(rpc, "resolve_guest", lambda *_: SimpleNamespace(id="owned"))
    monkeypatch.setattr(rpc, "_execute", execute)
    monkeypatch.setattr(agent_manager, "docker_client", running_client())
    monkeypatch.setattr(agent_manager, "_agent_dir", lambda _agent_id: tmp_path)
    monkeypatch.setattr(openclaw_gateway_config, "prepare_native_gateway_files", lambda _directory: None)
    result = await rpc.execute_gateway_turn(agent, message)
    if expected == "bootstrap_pending":
        assert result == rpc.NotAdmittedOutcome(str(message.id), "not_submitted", expected)
    else:
        assert result == rpc.UncertainOutcome(str(message.id), "unknown", None, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["changed", "missing", "foreign"])
async def test_unowned_or_changed_guest_is_never_submitted(monkeypatch, condition):
    agent = AgentRecord(id=uuid4(), name="test", creator_id=uuid4(), tenant_id=uuid4())
    message = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="hello")

    @asynccontextmanager
    async def exclusion(_agent_id):
        yield

    async def allowed(_agent_id):
        return None

    async def fresh(_agent_id):
        if condition == "changed":
            return AgentRecord(id=agent.id, name=agent.name, creator_id=uuid4(), tenant_id=agent.tenant_id)
        return agent

    def inspect(*_args):
        if condition == "foreign":
            raise GuestOwnershipError(agent.id, "foreign guest", str(uuid4()))

    monkeypatch.setattr(rpc, "guest_write_section", exclusion)
    monkeypatch.setattr(rpc, "assert_work_allowed", allowed)
    monkeypatch.setattr(rpc.agent_dao, "get", fresh)
    monkeypatch.setattr(rpc, "resolve_guest", inspect)
    monkeypatch.setattr(agent_manager, "docker_client", running_client())
    result = await rpc.execute_gateway_turn(agent, message)
    code = "agent_changed" if condition == "changed" else "guest_unavailable"
    assert result == rpc.NotAdmittedOutcome(str(message.id), "not_submitted", code)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["staging", "collision", "fdopen", "write", "execute"])
async def test_filesystem_failure_is_not_submitted_only_before_execute(monkeypatch, tmp_path, failure):
    agent = AgentRecord(id=uuid4(), name="test", creator_id=uuid4(), tenant_id=uuid4())
    message = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="hello")
    payload_id = uuid4()
    payload_path = tmp_path / f".maraclaw-rpc-{payload_id}.json"
    opened_fds = []
    executed = []
    real_fdopen = os.fdopen

    @asynccontextmanager
    async def exclusion(_agent_id):
        yield

    async def allowed(_agent_id):
        return None

    async def fresh(_agent_id):
        return agent

    def prepare(_directory):
        if failure == "staging":
            raise OSError("staging failed")

    def fdopen(fd, *args, **kwargs):
        opened_fds.append(fd)
        if failure == "fdopen":
            raise OSError("fdopen failed")
        return real_fdopen(fd, *args, **kwargs)

    def write_failure(*_args):
        raise OSError("write failed")

    def execute(*_args):
        executed.append(True)
        raise OSError("execute failed")

    if failure == "collision":
        payload_path.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(rpc, "guest_write_section", exclusion)
    monkeypatch.setattr(rpc, "assert_work_allowed", allowed)
    monkeypatch.setattr(rpc.agent_dao, "get", fresh)
    monkeypatch.setattr(rpc, "resolve_guest", lambda *_: SimpleNamespace(id="owned"))
    monkeypatch.setattr(rpc, "uuid4", lambda: payload_id)
    monkeypatch.setattr(rpc, "_execute", execute)
    monkeypatch.setattr(agent_manager, "docker_client", running_client())
    monkeypatch.setattr(agent_manager, "_agent_dir", lambda _agent_id: tmp_path)
    monkeypatch.setattr(openclaw_gateway_config, "prepare_native_gateway_files", prepare)
    monkeypatch.setattr(rpc.os, "fdopen", fdopen)
    if failure == "write":
        monkeypatch.setattr(rpc.json, "dump", write_failure)

    if failure == "execute":
        with pytest.raises(OSError, match="execute failed"):
            await rpc.execute_gateway_turn(agent, message)
        assert executed == [True]
    else:
        result = await rpc.execute_gateway_turn(agent, message)
        assert result == rpc.NotAdmittedOutcome(str(message.id), "not_submitted", "filesystem_error")
        assert not executed

    if failure in {"fdopen", "write"}:
        assert len(opened_fds) == 1
        with pytest.raises(OSError) as exc:
            os.fstat(opened_fds[0])
        assert exc.value.errno == errno.EBADF
    if failure == "collision":
        assert payload_path.read_text(encoding="utf-8") == "existing"
    else:
        assert not payload_path.exists()


@pytest.mark.asyncio
async def test_repeated_cancellation_keeps_payload_and_exclusion_until_execute_ends(monkeypatch, tmp_path):
    agent = AgentRecord(id=uuid4(), name="test", creator_id=uuid4(), tenant_id=uuid4())
    message = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="hello")
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = Event()
    held = False
    payload = None

    @asynccontextmanager
    async def exclusion(_agent_id):
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False

    async def allowed(_agent_id):
        return None

    async def fresh(_agent_id):
        return agent

    def execute(_container_id, payload_name, cancel):
        nonlocal payload
        payload = tmp_path / payload_name
        loop.call_soon_threadsafe(started.set)
        assert cancel.wait(5)
        loop.call_soon_threadsafe(cancelled.set)
        assert release.wait(5)
        return "{}"

    monkeypatch.setattr(rpc, "guest_write_section", exclusion)
    monkeypatch.setattr(rpc, "assert_work_allowed", allowed)
    monkeypatch.setattr(rpc.agent_dao, "get", fresh)
    monkeypatch.setattr(rpc, "resolve_guest", lambda *_: SimpleNamespace(id="owned"))
    monkeypatch.setattr(rpc, "_execute", execute)
    monkeypatch.setattr(agent_manager, "docker_client", running_client())
    monkeypatch.setattr(agent_manager, "_agent_dir", lambda _agent_id: tmp_path)
    monkeypatch.setattr(openclaw_gateway_config, "prepare_native_gateway_files", lambda _directory: None)
    task = asyncio.create_task(rpc.execute_gateway_turn(agent, message))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.wait_for(cancelled.wait(), 5)
        task.cancel()
        turn = loop.create_future()
        loop.call_soon(turn.set_result, None)
        await asyncio.wait_for(turn, 5)
        assert not task.done()
        assert held
        assert payload is not None
        assert payload.exists()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert not held
    assert payload is not None
    assert not payload.exists()


class _Turn:
    """One execute_gateway_turn harness: a counted work gate, a counted guest resolution and a fake exec."""

    def __init__(self, monkeypatch, tmp_path, *, now=lambda: 0.0, exec_error: Exception | None = None):
        self.agent = AgentRecord(id=uuid4(), name="test", creator_id=uuid4(), tenant_id=uuid4())
        self.gate_calls = 0
        self.gate_error: Exception | None = None
        self.resolutions = 0
        self.executed = 0
        self.exec_error = exec_error
        self.cache = GuestResolutionCache(30.0, now)

        @asynccontextmanager
        async def exclusion(_agent_id):
            yield

        async def gate(_agent_id):
            self.gate_calls += 1
            if self.gate_error is not None:
                raise self.gate_error

        async def fresh(_agent_id):
            return self.agent

        def resolve(*_args):
            self.resolutions += 1
            return SimpleNamespace(id="owned")

        def execute(_container_id, _payload_name, _cancel):
            self.executed += 1
            if self.exec_error is not None:
                raise self.exec_error
            return wire_terminal(self.message_id)

        self.message = GatewayMessageRecord(id=uuid4(), agent_id=self.agent.id, content="hello")
        self.message_id = str(self.message.id)
        monkeypatch.setattr(rpc, "guest_write_section", exclusion)
        monkeypatch.setattr(rpc, "assert_work_allowed", gate)
        monkeypatch.setattr(rpc.agent_dao, "get", fresh)
        monkeypatch.setattr(rpc, "resolve_guest", resolve)
        monkeypatch.setattr(rpc, "guest_resolutions", self.cache)
        monkeypatch.setattr(rpc, "_execute", execute)
        monkeypatch.setattr(agent_manager, "docker_client", running_client())
        monkeypatch.setattr(agent_manager, "_agent_dir", lambda _agent_id: tmp_path)
        monkeypatch.setattr(openclaw_gateway_config, "prepare_native_gateway_files", lambda _directory: None)
        self.storage = LocalStorageBackend(str(tmp_path / "storage"))
        self.users: dict[UUID, UserRecord] = {}

        async def lookup(user_id):
            return self.users.get(user_id)

        monkeypatch.setattr(onboarding_greeting, "get_storage_backend", lambda: self.storage)
        monkeypatch.setattr(user_dao, "get", lookup)

    async def run(self):
        return await rpc.execute_gateway_turn(self.agent, self.message)


@pytest.mark.parametrize("streaming", [False, True])
async def test_native_inspects_and_exec_use_dedicated_capacity(monkeypatch, tmp_path, streaming):
    monkeypatch.setattr(get_settings(), "OPENCLAW_PROCESS_MAX_PARALLEL_TURNS", 1)
    turn = _Turn(monkeypatch, tmp_path)
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = Event()
    threads: list[str] = []

    def resolve(*_args):
        threads.append(current_thread().name)
        return SimpleNamespace(id="owned")

    def inspect(_reference):
        threads.append(current_thread().name)
        return SimpleNamespace(state=SimpleNamespace(running=True))

    def execute(*_args):
        threads.append(current_thread().name)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return wire_terminal(turn.message_id)

    from unittest.mock import AsyncMock

    monkeypatch.setattr(rpc, "streams_to_web", AsyncMock(return_value=streaming))
    monkeypatch.setattr(rpc, "response_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(rpc, "resolve_guest", resolve)
    client = running_client()
    monkeypatch.setattr(client.container, "inspect", inspect)
    monkeypatch.setattr(agent_manager, "docker_client", client)
    monkeypatch.setattr(rpc, "_execute", execute)
    monkeypatch.setattr(rpc.DeltaRelay, "execute", lambda *_args: execute())
    capacity = native_capacity()
    assert capacity.executor is None
    tasks = [asyncio.create_task(turn.run())]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        tasks.append(asyncio.create_task(turn.run()))
        default_thread = await asyncio.wait_for(asyncio.to_thread(lambda: current_thread().name), 1)
        assert not default_thread.startswith("openclaw-native")
        assert len(threads) == 3
        assert all(name.startswith("openclaw-native") for name in threads)
    finally:
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks), 5)
    assert all(isinstance(outcome, rpc.TerminalOutcome) for outcome in outcomes)


@pytest.mark.parametrize("error", [ObservationTimeout(), ClientNotFoundError(), ParsingError()])
async def test_resolution_failure_is_not_submitted(monkeypatch, tmp_path, error):
    turn = _Turn(monkeypatch, tmp_path)

    def resolve(*_args):
        raise error

    monkeypatch.setattr(rpc, "resolve_guest", resolve)
    assert await turn.run() == rpc.NotAdmittedOutcome(turn.message_id, "not_submitted", "guest_unavailable")
    assert turn.executed == 0
    assert not list(tmp_path.glob(".maraclaw-rpc-*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("condition", ["stopped", "inspect_error", "exec_error"])
async def test_fresh_guest_state_gates_execution(monkeypatch, tmp_path, streaming, cached, condition):
    turn = _Turn(monkeypatch, tmp_path)
    inspections = []
    executed = []

    async def streams(_message):
        return streaming

    async def guard(*_args):
        return None

    def inspect(reference):
        inspections.append(reference)
        if condition == "inspect_error":
            raise DockerException(["docker", "inspect"], 1, stderr=b"unavailable")
        return SimpleNamespace(state=SimpleNamespace(running=condition != "stopped"))

    def execute(*_args):
        executed.append(True)
        raise DockerException(["docker", "exec"], 1, stderr=b"execution failed")

    if cached:
        turn.cache.put(turn.agent, GuestIdentity("owned", "owned", str(turn.agent.id), ()))
    monkeypatch.setattr(rpc, "streams_to_web", streams)
    monkeypatch.setattr(rpc, "response_guard", guard)
    monkeypatch.setattr(rpc, "_execute", execute)
    monkeypatch.setattr(rpc.DeltaRelay, "execute", execute)
    monkeypatch.setattr(agent_manager, "docker_client", SimpleNamespace(container=SimpleNamespace(inspect=inspect)))

    outcome = await turn.run()
    assert inspections == ["owned"]
    if condition == "exec_error":
        assert outcome == rpc.UncertainOutcome(turn.message_id, "unknown", None, "exec_failed")
        assert executed == [True]
    else:
        assert outcome == rpc.NotAdmittedOutcome(turn.message_id, "not_submitted", "guest_unavailable")
        assert executed == []
        assert not list(tmp_path.glob(".maraclaw-rpc-*.json"))


@pytest.mark.asyncio
async def test_staged_payload_carries_the_identity_prompt_for_every_native_turn(monkeypatch, tmp_path):
    turn = _Turn(monkeypatch, tmp_path)
    staged = {}

    def execute(_container_id, payload_name, _cancel):
        staged.update(json.loads((tmp_path / payload_name).read_text()))
        return wire_terminal(turn.message_id)

    monkeypatch.setattr(rpc, "_execute", execute)
    assert isinstance(await turn.run(), rpc.TerminalOutcome)
    assert get_settings().APP_NAME in staged["extraSystemPrompt"]
    assert staged["message"] == "hello"


@pytest.mark.asyncio
async def test_exactly_one_work_gate_check_guards_the_exec(monkeypatch, tmp_path):
    turn = _Turn(monkeypatch, tmp_path)
    assert isinstance(await turn.run(), rpc.TerminalOutcome)
    assert (turn.gate_calls, turn.executed) == (1, 1)


@pytest.mark.asyncio
async def test_cutover_started_after_the_claim_still_refuses_the_exec(monkeypatch, tmp_path):
    turn = _Turn(monkeypatch, tmp_path)
    turn.gate_error = OpenClawUpdateInProgressError()
    with pytest.raises(OpenClawUpdateInProgressError):
        await turn.run()
    assert (turn.gate_calls, turn.executed) == (1, 0)
    assert not list(tmp_path.glob(".maraclaw-rpc-*.json"))


@pytest.mark.asyncio
async def test_guest_resolution_is_reused_within_the_ttl_and_refreshed_after_it(monkeypatch, tmp_path):
    clock = [0.0]
    turn = _Turn(monkeypatch, tmp_path, now=lambda: clock[0])
    for at in (0.0, 29.0):
        clock[0] = at
        assert isinstance(await turn.run(), rpc.TerminalOutcome)
    assert turn.resolutions == 1
    clock[0] = 30.0
    assert isinstance(await turn.run(), rpc.TerminalOutcome)
    assert turn.resolutions == 2


@pytest.mark.asyncio
async def test_exec_failure_drops_the_cached_guest(monkeypatch, tmp_path):
    turn = _Turn(monkeypatch, tmp_path, exec_error=DockerException(["docker", "exec"], 1, stderr=b"gone"))
    assert isinstance(await turn.run(), rpc.UncertainOutcome)
    assert isinstance(await turn.run(), rpc.UncertainOutcome)
    assert turn.resolutions == 2


@pytest.mark.asyncio
async def test_every_native_turn_rebuilds_identity_from_stored_records(monkeypatch, tmp_path):
    turn = _Turn(monkeypatch, tmp_path)
    turn.agent.name = "Mira Atlas"
    first, second = (UserRecord(id=uuid4(), display_name=name) for name in ("Linh Tran", "Omar Haddad"))
    creator = UserRecord(id=turn.agent.creator_id, display_name="Creator Quang")
    turn.users = {user.id: user for user in (first, second, creator)}
    await turn.storage.write_bytes(f"{turn.agent.id}/soul.md", b"# Personality\n\nSoul sentinel: dry-witted pilot.")
    staged: list[dict[str, str]] = []

    def execute(_container_id, payload_name, _cancel):
        staged.append(json.loads((tmp_path / payload_name).read_text()))
        return wire_terminal(turn.message_id)

    monkeypatch.setattr(rpc, "_execute", execute)
    for sender, content in ((first, "hi"), (second, "what is next?"), (None, "hello from a channel"), (first, "again")):
        turn.message = GatewayMessageRecord(
            id=uuid4(),
            agent_id=turn.agent.id,
            content=content,
            sender_user_id=sender.id if sender else None,
        )
        turn.message_id = str(turn.message.id)
        assert isinstance(await turn.run(), rpc.TerminalOutcome)

    prompts = [payload["extraSystemPrompt"] for payload in staged]
    assert [payload["message"] for payload in staged] == ["hi", "what is next?", "hello from a channel", "again"]
    assert all(get_settings().APP_NAME in prompt and "Mira Atlas" in prompt for prompt in prompts)
    assert all("Soul sentinel" in prompt for prompt in prompts)
    for prompt, sender in zip(prompts, (first, second, None, first), strict=True):
        named = {user.display_name for user in (first, second, creator) if user.display_name in prompt}
        assert named == ({sender.display_name} if sender else set())
        assert (str(sender.id) in prompt) if sender else all(str(user.id) not in prompt for user in turn.users.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [PermissionError("denied"), FileNotFoundError(), "symlink"])
async def test_unreadable_or_absent_soul_still_stages_the_turn_with_known_names(monkeypatch, tmp_path, failure):
    turn = _Turn(monkeypatch, tmp_path)
    turn.agent.name = "Mira Atlas"
    customer = UserRecord(id=uuid4(), display_name="Linh Tran")
    turn.users = {customer.id: customer}
    turn.message = GatewayMessageRecord(id=uuid4(), agent_id=turn.agent.id, content="hi", sender_user_id=customer.id)
    turn.message_id = str(turn.message.id)
    staged: dict[str, str] = {}

    async def unreadable(_key, _max_bytes):
        raise failure

    def execute(_container_id, payload_name, _cancel):
        staged.update(json.loads((tmp_path / payload_name).read_text()))
        return wire_terminal(turn.message_id)

    if failure == "symlink":
        soul = tmp_path / "storage" / str(turn.agent.id) / "soul.md"
        soul.parent.mkdir(parents=True, exist_ok=True)
        soul.symlink_to(tmp_path / "missing-soul")
    else:
        monkeypatch.setattr(turn.storage, "read_bytes_bounded", unreadable)
    monkeypatch.setattr(rpc, "_execute", execute)
    assert isinstance(await turn.run(), rpc.TerminalOutcome)
    assert "Mira Atlas" in staged["extraSystemPrompt"]
    assert "Linh Tran" in staged["extraSystemPrompt"]
