from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from app.core.json_types import JsonObject
from app.services import agent_tools
from app.services.agent_tool_exec.registry import ToolArguments, ToolOutputCallback
from app.services.sandbox.base import ExecutionResult
from app.services.sandbox.config import SandboxConfig
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
    assert result == "📤 Output:\nbackend output"
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
    assert result == "📤 Output:\npartial\n\n⚠️ Stderr:\nwarning\n\n❌ Error: timed out"
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
