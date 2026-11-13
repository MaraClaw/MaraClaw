from __future__ import annotations

import asyncio
import functools
import itertools
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.json_types import JsonObject
from app.services import agent_tools
from app.services.agent_tool_exec import code_exec
from app.services.agent_tool_exec.registry import ToolArguments, ToolOutputCallback
from app.services.sandbox.base import ExecutionDeadline, ExecutionResult, TerminationReason, active_execution_deadline
from app.services.sandbox.config import SandboxConfig, SandboxType
from app.services.sandbox.local.subprocess_backend import SubprocessBackend


class RecordingBackend:
    def __init__(self) -> None:
        self.formatter: SubprocessBackend = SubprocessBackend(SandboxConfig())
        self.calls: list[tuple[str, str, int, str | None, uuid.UUID | None]] = []
        self.result: ExecutionResult = ExecutionResult(True, "backend output", "", 0, 1)
        self.failure: Exception | None = None

    async def execute(
        self,
        code: str,
        language: str,
        exec_timeout: int = 30,
        work_dir: str | None = None,
        *,
        on_output: ToolOutputCallback | None = None,
        agent_id: uuid.UUID | None = None,
        **kwargs: object,
    ) -> ExecutionResult:
        self.calls.append((code, language, exec_timeout, work_dir, agent_id))
        if on_output:
            pending = on_output("partial output", "stdout")
            if pending is not None:
                await pending
        if self.failure:
            raise self.failure
        return self.result

    def _format_result(self, result: ExecutionResult) -> str:
        return self.formatter._format_result(result)


@pytest.fixture(autouse=True)
def host_launches(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    launches: list[bool] = []

    async def launch(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        launches.append(True)
        raise AssertionError("code tool must not launch a host subprocess outside its backend")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    return launches


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> RecordingBackend:
    backend = RecordingBackend()

    async def get_tool_config(agent_id: uuid.UUID | None, tool_name: str) -> JsonObject | None:
        return None

    monkeypatch.setattr("app.config.get_sandbox_config", lambda: SandboxConfig(max_timeout=7))
    monkeypatch.setattr("app.services.sandbox.registry.get_sandbox_backend", lambda config: backend)
    monkeypatch.setattr(agent_tools, "_get_tool_config", get_tool_config)
    return backend


class ParkedBackend:
    """Runs until the test releases it; with ``shielded`` it also outlives cancellation until released."""

    def __init__(self, name: str, log: list[str]) -> None:
        self.formatter: SubprocessBackend = SubprocessBackend(SandboxConfig())
        self.name: str = name
        self.log: list[str] = log
        self.started: asyncio.Event = asyncio.Event()
        self.release: asyncio.Event = asyncio.Event()
        self.shielded: bool = False
        self.deadline: ExecutionDeadline | None = None
        self.tasks: list[asyncio.Task[ExecutionResult]] = []

    async def execute(self, **kwargs: object) -> ExecutionResult:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.append(task)
        self.deadline = active_execution_deadline.get()
        self.log.append(f"start {self.name}")
        self.started.set()
        try:
            await self.release.wait()
        finally:
            if self.shielded:
                await self.release.wait()
            self.log.append(f"end {self.name}")
        return ExecutionResult(True, self.name, "", 0, 1)

    def _format_result(self, result: ExecutionResult) -> str:
        return self.formatter._format_result(result)


@pytest.fixture(autouse=True)
def limits(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    # Each test gets its own process-wide slot pool and the shipped defaults, so no slot state crosses tests.
    limits = SimpleNamespace(SANDBOX_MAX_CONCURRENT_EXECUTIONS=8, SANDBOX_EXECUTION_QUEUE_WAIT_SECONDS=10.0)
    monkeypatch.setattr("app.config.get_settings", lambda: limits)
    monkeypatch.setattr(code_exec, "_EXECUTION_SLOTS", code_exec._ExecutionSlots())
    return limits


@pytest.fixture
def parked(monkeypatch: pytest.MonkeyPatch) -> list[ParkedBackend]:
    """Backends handed out in selection order; a test appends one per expected selection."""
    backends: list[ParkedBackend] = []
    selections = itertools.count()

    async def get_tool_config(agent_id: uuid.UUID | None, tool_name: str) -> JsonObject | None:
        return None

    monkeypatch.setattr("app.config.get_sandbox_config", SandboxConfig)
    monkeypatch.setattr("app.services.sandbox.registry.get_sandbox_backend", lambda config: backends[next(selections)])
    monkeypatch.setattr(agent_tools, "_get_tool_config", get_tool_config)
    return backends


def queued_callers() -> int:
    return len(code_exec._EXECUTION_SLOTS._waiters)


@pytest.mark.parametrize("tool_name", ["execute_code", "execute_code_e2b"])
async def test_executes_configured_backend_with_agent_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: RecordingBackend, tool_name: str
) -> None:
    # Given: overrides differ from platform policy and the caller supplies a streaming callback.
    agent_id = uuid.uuid4()
    configs: list[SandboxConfig] = []
    lookups: list[tuple[uuid.UUID | None, str]] = []
    chunks: list[tuple[str, str]] = []

    async def get_tool_config(received_id: uuid.UUID | None, received_name: str) -> JsonObject:
        lookups.append((received_id, received_name))
        return {"max_timeout": 3, "allow_network": True}

    def select(config: SandboxConfig) -> RecordingBackend:
        configs.append(config)
        return backend

    async def on_output(content: str, label: str = "stdout") -> None:
        chunks.append((content, label))

    monkeypatch.setattr(agent_tools, "_get_tool_config", get_tool_config)
    monkeypatch.setattr("app.services.sandbox.registry.get_sandbox_backend", select)
    # When: dispatch through the retained facade.
    result = await agent_tools._execute_code(
        agent_id, tmp_path, {"code": "print('ok')", "timeout": 8}, tool_name=tool_name, on_output=on_output
    )
    # Then: the override wins and execution/streaming happens once in the backend.
    assert result == "📤 Output:\nbackend output\n\nTermination: completed\n\nStreamed output dropped: no"
    assert lookups == [(agent_id, tool_name)]
    assert len(configs) == 1
    assert configs[0].allow_network is True
    assert configs[0].max_timeout == 3
    assert backend.calls == [("print('ok')", "python", 3, str(tmp_path), agent_id)]
    assert chunks == [("partial output", "stdout")]


@pytest.mark.parametrize("tool_name", ["execute_code", "execute_code_e2b"])
@pytest.mark.parametrize("stage", ["platform", "agent", "selection"])
async def test_configuration_error_never_launches_host_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    backend: RecordingBackend,
    host_launches: list[bool],
    tool_name: str,
    stage: str,
) -> None:
    # Given: one configuration boundary rejects execution.
    def fail_platform() -> SandboxConfig:
        raise ValueError("invalid sandbox")

    async def fail_agent(agent_id: uuid.UUID | None, tool_name: str) -> JsonObject | None:
        raise ValueError("invalid sandbox")

    def fail_selection(config: SandboxConfig) -> RecordingBackend:
        raise ValueError("invalid sandbox")

    match stage:
        case "platform":
            monkeypatch.setattr("app.config.get_sandbox_config", fail_platform)
        case "agent":
            monkeypatch.setattr(agent_tools, "_get_tool_config", fail_agent)
        case "selection":
            monkeypatch.setattr("app.services.sandbox.registry.get_sandbox_backend", fail_selection)
        case _:
            pytest.fail("unknown configuration stage")
    # When
    result = await agent_tools._execute_code(None, tmp_path, {"code": "pass"}, tool_name=tool_name)
    # Then: the error surfaces without backend execution or host launch.
    assert "sandbox configuration error: invalid sandbox" in result.lower()
    assert backend.calls == []
    assert host_launches == []


@pytest.mark.parametrize("tool_name", ["execute_code", "execute_code_e2b"])
@pytest.mark.parametrize("failure", [RuntimeError("backend boom"), TimeoutError("backend timed out")])
async def test_partial_execution_failure_is_not_retried(
    tmp_path: Path, backend: RecordingBackend, host_launches: list[bool], tool_name: str, failure: Exception
) -> None:
    # Given: the backend emits output before failing.
    backend.failure = failure
    chunks: list[str] = []

    async def on_output(content: str, label: str = "stdout") -> None:
        chunks.append(content)

    # When
    result = await agent_tools._execute_code(None, tmp_path, {"code": "pass"}, tool_name=tool_name, on_output=on_output)
    # Then: execution is single-shot even after externally visible partial output.
    assert f"execution error: {failure}" in result.lower()
    assert len(backend.calls) == 1
    assert chunks == ["partial output"]
    assert host_launches == []


@pytest.mark.parametrize(
    ("arguments", "expected_timeout"),
    [({"code": "pass", "timeout": 2}, 2), ({"code": "pass", "timeout": 99}, 7), ({"code": "pass"}, 7)],
)
async def test_platform_timeout_policy_is_forwarded(
    tmp_path: Path, backend: RecordingBackend, arguments: ToolArguments, expected_timeout: int
) -> None:
    # Given: platform config is used without per-agent overrides.
    # When
    await agent_tools._execute_code(None, tmp_path, arguments)
    # Then
    assert backend.calls == [("pass", "python", expected_timeout, str(tmp_path), None)]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, "No code provided"),
        ({"code": "  "}, "No code provided"),
        ({"code": 1}, "No code provided"),
        ({"code": "pass", "language": "ruby"}, "Unsupported language: ruby"),
    ],
)
async def test_invalid_inputs_do_not_execute(
    tmp_path: Path, backend: RecordingBackend, arguments: ToolArguments, expected: str
) -> None:
    # Given: invalid boundary input.
    # When
    result = await agent_tools._execute_code(None, tmp_path, arguments)
    # Then
    assert expected in result
    assert backend.calls == []


async def test_backend_timeout_result_preserves_partial_output(tmp_path: Path, backend: RecordingBackend) -> None:
    # Given: a backend owns timeout enforcement and returns a failed result.
    backend.result = ExecutionResult(False, "partial", "warning", -1, 2, error="timed out")
    # When
    result = await agent_tools._execute_code(None, tmp_path, {"code": "pass"})
    # Then
    assert result == (
        "📤 Output:\npartial\n\n⚠️ Stderr:\nwarning\n\n❌ Error: timed out"
        "\n\nTermination: completed\n\nStreamed output dropped: no"
    )
    assert len(backend.calls) == 1


@pytest.mark.parametrize("tool_name", ["execute_code", "execute_code_e2b"])
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"max_stdout_bytes": 0}, (0, 43)),
        ({"max_stderr_bytes": 7}, (29, 7)),
        ({"max_stdout_bytes": 11, "max_stderr_bytes": 13}, (11, 13)),
        ({"max_stdout_bytes": True, "max_stderr_bytes": "3"}, (29, 43)),
        ({"max_stdout_bytes": None, "max_stderr_bytes": 1.5}, (29, 43)),
    ],
)
async def test_output_budget_overrides_reach_selected_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    backend: RecordingBackend,
    tool_name: str,
    overrides: JsonObject,
    expected: tuple[int, int],
) -> None:
    # Given: each platform limit differs from the agent's supplied override.
    configs: list[SandboxConfig] = []

    async def get_tool_config(_: uuid.UUID | None, __: str) -> JsonObject:
        return overrides

    def select(config: SandboxConfig) -> RecordingBackend:
        configs.append(config)
        return backend

    monkeypatch.setattr(
        "app.config.get_sandbox_config", lambda: SandboxConfig(max_stdout_bytes=29, max_stderr_bytes=43)
    )
    monkeypatch.setattr(agent_tools, "_get_tool_config", get_tool_config)
    monkeypatch.setattr("app.services.sandbox.registry.get_sandbox_backend", select)
    # When
    await agent_tools._execute_code(None, tmp_path, {"code": "pass"}, tool_name=tool_name)
    # Then: missing or non-integer JSON fields inherit independently, including zero overrides.
    assert [(config.max_stdout_bytes, config.max_stderr_bytes) for config in configs] == [expected]
    assert len(backend.calls) == 1


@pytest.mark.parametrize("field", ["max_stdout_bytes", "max_stderr_bytes"])
async def test_negative_output_budget_prevents_backend_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    backend: RecordingBackend,
    host_launches: list[bool],
    field: str,
) -> None:
    # Given: a typed but invalid per-agent limit.
    async def get_tool_config(_: uuid.UUID | None, __: str) -> JsonObject:
        return {field: -1}

    monkeypatch.setattr(agent_tools, "_get_tool_config", get_tool_config)
    # When
    result = await agent_tools._execute_code(None, tmp_path, {"code": "pass"})
    # Then: validation fails closed, without a backend call or host retry.
    assert "sandbox configuration error" in result.lower()
    assert backend.calls == []
    assert host_launches == []


@pytest.mark.parametrize("stalled_cleanup", [False, True])
async def test_outer_guard_bounds_execution_and_cancellation_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: RecordingBackend, stalled_cleanup: bool
) -> None:
    from app.services.sandbox import base

    monkeypatch.setattr(base, "CLEANUP_GRACE_SECONDS", 0.1)
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    tasks: list[asyncio.Task[ExecutionResult]] = []

    async def stalled(*args: object, **kwargs: object) -> ExecutionResult:
        task = asyncio.current_task()
        assert task is not None
        tasks.append(task)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
            if stalled_cleanup:
                await release.wait()
        return backend.result

    monkeypatch.setattr(backend, "execute", stalled)
    try:
        async with asyncio.timeout(1):
            result = await agent_tools._execute_code(None, tmp_path, {"code": "pass", "timeout": 0})
        assert started.is_set()
        assert cancelled.is_set()
        assert "Termination: backend abort" in result
        assert ("cleanup incomplete" in result) == stalled_cleanup
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("reason", list(TerminationReason))
@pytest.mark.parametrize("dropped", [False, True])
def test_formatter_reports_termination_and_dropped_streamed_output(reason: TerminationReason, dropped: bool) -> None:
    backend = SubprocessBackend(SandboxConfig())
    result = backend._format_result(
        ExecutionResult(False, "captured", "", 1, 0, termination=reason, output_dropped=dropped)
    )
    assert reason.value.replace("_", " ") in result.lower()
    assert "captured" in result
    assert ("Streamed output was dropped" in result) == dropped
    assert ("Streamed output dropped: no" in result) != dropped


async def test_cap_admits_n_executions_across_agents_and_backend_types(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, limits: SimpleNamespace
) -> None:
    # Given: a cap of two and three agents whose tool configs select three different backend types.
    limits.SANDBOX_MAX_CONCURRENT_EXECUTIONS = 2
    log: list[str] = []
    types = {uuid.uuid4(): SandboxType.SUBPROCESS, uuid.uuid4(): SandboxType.DOCKER, uuid.uuid4(): SandboxType.E2B}
    backends = {sandbox_type: ParkedBackend(sandbox_type.value, log) for sandbox_type in types.values()}

    async def get_tool_config(agent_id: uuid.UUID | None, tool_name: str) -> JsonObject:
        assert agent_id is not None
        return {"sandbox_type": types[agent_id].value}

    monkeypatch.setattr("app.config.get_sandbox_config", SandboxConfig)
    monkeypatch.setattr("app.services.sandbox.registry.get_sandbox_backend", lambda config: backends[config.type])
    monkeypatch.setattr(agent_tools, "_get_tool_config", get_tool_config)
    first, second, third = backends.values()
    # When: all three agents execute at once.
    calls = [asyncio.create_task(agent_tools._execute_code(agent, tmp_path, {"code": "pass"})) for agent in types]
    try:
        async with asyncio.timeout(1):
            _ = await first.started.wait()
            _ = await second.started.wait()
        # Then: two backends run and the third caller waits in the queue.
        assert log == ["start subprocess", "start docker"]
        assert queued_callers() == 1
        first.release.set()
        async with asyncio.timeout(1):
            _ = await third.started.wait()
        # The third backend starts only after a running one has finished.
        assert log == ["start subprocess", "start docker", "end subprocess", "start e2b"]
    finally:
        for backend in backends.values():
            backend.release.set()
        results = await asyncio.gather(*calls)
    assert [backend.name in result for backend, result in zip(backends.values(), results, strict=True)] == [True] * 3


async def test_queue_wait_expiry_returns_busy_without_launching(
    tmp_path: Path, parked: list[ParkedBackend], limits: SimpleNamespace
) -> None:
    # Given: the only slot is busy and the queue wait is short.
    limits.SANDBOX_MAX_CONCURRENT_EXECUTIONS = 1
    limits.SANDBOX_EXECUTION_QUEUE_WAIT_SECONDS = 0.01
    log: list[str] = []
    holder, late = ParkedBackend("holder", log), ParkedBackend("late", log)
    parked.extend((holder, late))
    running = asyncio.create_task(agent_tools._execute_code(None, tmp_path, {"code": "pass"}))
    try:
        async with asyncio.timeout(1):
            _ = await holder.started.wait()
            # When: a second call waits past its queue budget.
            result = await agent_tools._execute_code(None, tmp_path, {"code": "pass"})
        # Then: it gets the busy result, no backend launch and no lingering queue entry.
        assert result == code_exec.SANDBOX_BUSY_MESSAGE
        assert log == ["start holder"]
        assert queued_callers() == 0
        # The busy result is not mistaken for any termination of an execution that ran.
        assert [reason.value.replace("_", " ") in result.lower() for reason in TerminationReason] == [False] * len(
            TerminationReason
        )
    finally:
        holder.release.set()
        _ = await running


async def test_zero_queue_wait_refuses_without_waiting(
    tmp_path: Path, parked: list[ParkedBackend], limits: SimpleNamespace
) -> None:
    # Given: the only slot is busy and queueing is disabled.
    limits.SANDBOX_MAX_CONCURRENT_EXECUTIONS = 1
    limits.SANDBOX_EXECUTION_QUEUE_WAIT_SECONDS = 0
    log: list[str] = []
    holder, late = ParkedBackend("holder", log), ParkedBackend("late", log)
    parked.extend((holder, late))
    running = asyncio.create_task(agent_tools._execute_code(None, tmp_path, {"code": "pass"}))
    try:
        async with asyncio.timeout(1):
            _ = await holder.started.wait()
        # When: the next call is driven one step, without yielding to the event loop.
        pending = agent_tools._execute_code(None, tmp_path, {"code": "pass"})
        with pytest.raises(StopIteration) as finished:
            _ = pending.send(None)
        # Then: it completed in that step with the busy result and launched nothing.
        assert finished.value.value == code_exec.SANDBOX_BUSY_MESSAGE
        assert log == ["start holder"]
    finally:
        holder.release.set()
        _ = await running


async def test_time_spent_queued_does_not_shrink_the_execution_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, parked: list[ParkedBackend], limits: SimpleNamespace
) -> None:
    # Given: deadlines read an injected clock and the only slot is busy.
    limits.SANDBOX_MAX_CONCURRENT_EXECUTIONS = 1
    now = [100.0]
    monkeypatch.setattr(code_exec, "ExecutionDeadline", functools.partial(ExecutionDeadline, clock=lambda: now[0]))
    log: list[str] = []
    holder, queued = ParkedBackend("holder", log), ParkedBackend("queued", log)
    parked.extend((holder, queued))
    calls = [asyncio.create_task(agent_tools._execute_code(None, tmp_path, {"code": "pass", "timeout": 5}))]
    calls.append(asyncio.create_task(agent_tools._execute_code(None, tmp_path, {"code": "pass", "timeout": 5})))
    try:
        async with asyncio.timeout(1):
            _ = await holder.started.wait()
        assert queued_callers() == 1
        # When: the second caller spends 1000 clock seconds queued before the slot frees.
        now[0] += 1000
        holder.release.set()
        async with asyncio.timeout(1):
            _ = await queued.started.wait()
    finally:
        holder.release.set()
        queued.release.set()
        _ = await asyncio.gather(*calls)
    # Then: its deadline starts at admission and still grants the full five seconds.
    assert queued.deadline is not None
    assert (queued.deadline.started_at, queued.deadline.remaining()) == (1100.0, 5.0)


async def test_abandoned_execution_keeps_its_slot_until_the_backend_finishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, parked: list[ParkedBackend], limits: SimpleNamespace
) -> None:
    from app.services.sandbox import base

    # Given: one slot, no queueing, and a backend that ignores cancellation until released.
    monkeypatch.setattr(base, "CLEANUP_GRACE_SECONDS", 0.1)
    limits.SANDBOX_MAX_CONCURRENT_EXECUTIONS = 1
    limits.SANDBOX_EXECUTION_QUEUE_WAIT_SECONDS = 0
    log: list[str] = []
    abandoned, refused, admitted = (ParkedBackend(name, log) for name in ("abandoned", "refused", "admitted"))
    abandoned.shielded = True
    admitted.release.set()
    parked.extend((abandoned, refused, admitted))
    try:
        # When: the call gives up on the backend past its hard deadline.
        async with asyncio.timeout(1):
            result = await agent_tools._execute_code(None, tmp_path, {"code": "pass", "timeout": 0})
        assert TerminationReason.BACKEND_ABORT.value.replace("_", " ") in result.lower()
        assert not abandoned.tasks[0].done()
        # Then: its slot stays taken while the backend still runs.
        assert await agent_tools._execute_code(None, tmp_path, {"code": "pass"}) == code_exec.SANDBOX_BUSY_MESSAGE
    finally:
        abandoned.release.set()
        _ = await asyncio.gather(*abandoned.tasks, return_exceptions=True)
    # And the slot frees once that backend task really ends.
    result = await agent_tools._execute_code(None, tmp_path, {"code": "pass"})
    assert "admitted" in result
    assert log == ["start abandoned", "end abandoned", "start admitted", "end admitted"]


async def test_configuration_error_neither_waits_for_nor_holds_a_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, parked: list[ParkedBackend], limits: SimpleNamespace
) -> None:
    # Given: the only slot is busy, queueing is disabled, and the agent's tool config is invalid.
    limits.SANDBOX_MAX_CONCURRENT_EXECUTIONS = 1
    limits.SANDBOX_EXECUTION_QUEUE_WAIT_SECONDS = 0
    log: list[str] = []
    holder = ParkedBackend("holder", log)
    parked.append(holder)
    running = asyncio.create_task(agent_tools._execute_code(None, tmp_path, {"code": "pass"}))

    async def invalid(agent_id: uuid.UUID | None, tool_name: str) -> JsonObject:
        return {"max_stdout_bytes": -1}

    try:
        async with asyncio.timeout(1):
            _ = await holder.started.wait()
        monkeypatch.setattr(agent_tools, "_get_tool_config", invalid)
        # When
        result = await agent_tools._execute_code(None, tmp_path, {"code": "pass"})
    finally:
        holder.release.set()
        _ = await running
    # Then: the configuration error wins over saturation.
    assert result.startswith("❌ Sandbox configuration error:")


@pytest.mark.parametrize("moment", ["queued", "handed_over", "resolved", "admitted"])
async def test_cancelled_caller_leaks_no_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, parked: list[ParkedBackend], limits: SimpleNamespace, moment: str
) -> None:
    # Given: both slots are busy and a third caller is queued.
    limits.SANDBOX_MAX_CONCURRENT_EXECUTIONS = 2
    log: list[str] = []
    first, second, cancelled, *after = (
        ParkedBackend(name, log) for name in ("first", "second", "cancelled", "after-1", "after-2")
    )
    parked.extend((first, second, cancelled, *after))
    holders = [asyncio.create_task(agent_tools._execute_code(None, tmp_path, {"code": "pass"})) for _ in range(2)]
    caller = asyncio.create_task(agent_tools._execute_code(None, tmp_path, {"code": "pass"}))
    try:
        async with asyncio.timeout(1):
            _ = await first.started.wait()
            _ = await second.started.wait()
        assert queued_callers() == 1
        # When: the caller is cancelled in the queue, while a freed slot is on its way to it, after the slot reached it
        # but before it resumed, or once admitted.
        async with asyncio.timeout(1):
            match moment:
                case "queued":
                    pass
                case "handed_over":
                    first.release.set()
                    _ = await first.tasks[0]
                case "resolved":
                    slots = code_exec._EXECUTION_SLOTS
                    hand_over, handed = slots._hand_over, asyncio.Event()

                    def hand_over_then_cancel(waiter: asyncio.Future[None]) -> None:
                        hand_over(waiter)
                        assert waiter.done()
                        assert not waiter.cancelled()
                        _ = caller.cancel()
                        handed.set()

                    monkeypatch.setattr(slots, "_hand_over", hand_over_then_cancel)
                    first.release.set()
                    _ = await handed.wait()
                case "admitted":
                    first.release.set()
                    _ = await cancelled.started.wait()
                case _:
                    pytest.fail("unknown cancellation moment")
        _ = caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
    finally:
        first.release.set()
        second.release.set()
        _ = await asyncio.gather(*holders)
    # Then: the queue is empty and two new executions are admitted at once.
    limits.SANDBOX_EXECUTION_QUEUE_WAIT_SECONDS = 0
    for backend in after:
        backend.release.set()
    results = await asyncio.gather(*(agent_tools._execute_code(None, tmp_path, {"code": "pass"}) for _ in after))
    assert queued_callers() == 0
    assert [backend.name in result for backend, result in zip(after, results, strict=True)] == [True, True]


@pytest.mark.parametrize("stage", ["launch", "execution"])
async def test_slot_is_released_when_the_backend_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: RecordingBackend, limits: SimpleNamespace, stage: str
) -> None:
    # Given: one slot, no queueing, and a backend that fails when called or while running.
    limits.SANDBOX_MAX_CONCURRENT_EXECUTIONS = 1
    limits.SANDBOX_EXECUTION_QUEUE_WAIT_SECONDS = 0

    def reject(**kwargs: object) -> ExecutionResult:
        raise TypeError("backend rejected arguments")

    match stage:
        case "launch":
            monkeypatch.setattr(backend, "execute", reject)
        case "execution":
            backend.failure = RuntimeError("backend boom")
        case _:
            pytest.fail("unknown failure stage")
    # When: two calls run one after the other.
    results = [await agent_tools._execute_code(None, tmp_path, {"code": "pass"}) for _ in range(2)]
    # Then: both reach the backend and report its error; the first failure did not keep the slot.
    assert [result.startswith("❌ Execution error:") for result in results] == [True, True]


def test_limiter_serves_successive_event_loops(
    tmp_path: Path, parked: list[ParkedBackend], limits: SimpleNamespace
) -> None:
    # Given: one slot shared by contending callers in two separate event loops.
    limits.SANDBOX_MAX_CONCURRENT_EXECUTIONS = 1
    log: list[str] = []

    async def contend() -> list[str]:
        holder, queued = ParkedBackend("holder", log), ParkedBackend("queued", log)
        parked.extend((holder, queued))
        calls = [asyncio.create_task(agent_tools._execute_code(None, tmp_path, {"code": "pass"})) for _ in range(2)]
        async with asyncio.timeout(1):
            _ = await holder.started.wait()
        assert queued_callers() == 1
        holder.release.set()
        queued.release.set()
        return await asyncio.gather(*calls)

    # When
    for _ in range(2):
        results = asyncio.run(contend())
        # Then: each loop serializes its callers through the queue and both run.
        assert ["holder" in results[0], "queued" in results[1]] == [True, True]
    assert log == ["start holder", "end holder", "start queued", "end queued"] * 2
