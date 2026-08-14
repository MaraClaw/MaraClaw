from __future__ import annotations

import asyncio
import uuid

import pytest

from app.services import agent_tools
from app.services.agent_tool_exec import code_exec
from app.services.sandbox.base import ExecutionResult
from app.services.sandbox.config import SandboxConfig

type SandboxCallValue = str | int | uuid.UUID | None


class FakeStream:
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        self.read_sizes: list[int] = []

    async def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self.chunks.pop(0) if self.chunks else b""


class FakeProcess:
    def __init__(self, stdout: list[bytes], stderr: list[bytes], returncode: int = 0):
        self.stdout = FakeStream(stdout)
        self.stderr = FakeStream(stderr)
        self.returncode = returncode
        self.killed = False

    async def wait(self) -> None:
        return None

    def kill(self) -> None:
        self.killed = True


class FakeBackend:
    def __init__(self, result: ExecutionResult | Exception):
        self.result = result
        self.calls: list[dict[str, SandboxCallValue]] = []

    async def execute(self, **kwargs) -> ExecutionResult:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def _format_result(self, result: ExecutionResult) -> str:
        return f"formatted: {result.stdout}"


@pytest.fixture(autouse=True)
def forbid_real_subprocess(monkeypatch):
    async def fail_real_subprocess(*_args, **_kwargs):
        raise AssertionError("tests must not create a real subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_real_subprocess)


@pytest.mark.parametrize(
    ("language", "code", "allow_network", "expected"),
    [
        ("bash", "sudo whoami", False, "❌ Blocked: dangerous command detected (sudo)"),
        ("bash", "curl https://example.test", False, "❌ Blocked: network command not allowed (curl)"),
        ("bash", "curl https://example.test", True, None),
        ("bash", "cat ../../secret", True, "❌ Blocked: directory traversal not allowed"),
        ("python", "os.system('id')", True, "❌ Blocked: unsafe operation detected (os.system)"),
        ("python", "import socket", False, "❌ Blocked: network operation not allowed (socket)"),
        ("python", "import socket", True, None),
        ("node", "process.exit(1)", True, "❌ Blocked: unsafe operation detected (process.exit)"),
        ("node", "require('http')", False, "❌ Blocked: network operation not allowed (require('http'))"),
        ("node", "console.log('ok')", False, None),
        ("unknown", "anything", False, None),
    ],
)
def test_check_code_safety_preserves_pattern_results(language, code, allow_network, expected):
    assert code_exec._check_code_safety(language, code, allow_network) == expected


async def test_agent_tools_execution_facades_delegate_to_extracted_module(monkeypatch, tmp_path):
    calls = []

    def check_code_safety(*args):
        calls.append(("safety", args))
        return "checked"

    async def execute_code(*args, **kwargs):
        calls.append(("execute", args, kwargs))
        return "executed"

    async def execute_code_legacy(*args, **kwargs):
        calls.append(("legacy", args, kwargs))
        return "legacy"

    monkeypatch.setattr(code_exec, "_check_code_safety", check_code_safety)
    monkeypatch.setattr(code_exec, "_execute_code", execute_code)
    monkeypatch.setattr(code_exec, "_execute_code_legacy", execute_code_legacy)

    agent_id = uuid.uuid4()
    assert agent_tools._check_code_safety("python", "pass") == "checked"
    assert (
        await agent_tools._execute_code(agent_id, tmp_path, {"code": "pass"}, tool_name="execute_code_e2b")
        == "executed"
    )
    assert await agent_tools._execute_code_legacy(tmp_path, {"code": "pass"}, True, 9) == "legacy"
    assert calls == [
        ("safety", ("python", "pass", False)),
        ("execute", (agent_id, tmp_path, {"code": "pass"}), {"tool_name": "execute_code_e2b", "on_output": None}),
        ("legacy", (tmp_path, {"code": "pass"}, True, 9, None), {}),
    ]


async def test_execute_code_uses_fake_sandbox_backend_and_runtime_facade_config(monkeypatch, tmp_path):
    backend = FakeBackend(ExecutionResult(True, "backend output", "", 0, 1))
    config = SandboxConfig(allow_network=False, max_timeout=4)

    async def get_tool_config(_agent_id, _tool_name):
        return {"max_timeout": 3, "allow_network": True}

    monkeypatch.setattr("app.config.get_sandbox_config", lambda: config)
    monkeypatch.setattr("app.services.sandbox.registry.get_sandbox_backend", lambda received: backend)
    monkeypatch.setattr(agent_tools, "_get_tool_config", get_tool_config)

    result = await code_exec._execute_code(uuid.uuid4(), tmp_path, {"code": "print('ok')", "timeout": 8})

    assert result == "formatted: backend output"
    assert backend.calls == [
        {
            "code": "print('ok')",
            "language": "python",
            "exec_timeout": 3,
            "work_dir": str(tmp_path.resolve()),
            "on_output": None,
            "agent_id": backend.calls[0]["agent_id"],
        }
    ]


async def test_execute_code_preserves_local_and_e2b_fallbacks(monkeypatch, tmp_path):
    config = SandboxConfig(allow_network=True, max_timeout=7)
    fallback_calls = []

    async def get_tool_config(_agent_id, _tool_name):
        raise ValueError("missing key")

    async def legacy(*args, **kwargs):
        fallback_calls.append((args, kwargs))
        return "legacy fallback"

    monkeypatch.setattr("app.config.get_sandbox_config", lambda: config)
    monkeypatch.setattr(agent_tools, "_get_tool_config", get_tool_config)
    monkeypatch.setattr(agent_tools, "_execute_code_legacy", legacy)

    assert await code_exec._execute_code(None, tmp_path, {"code": "pass"}) == "legacy fallback"
    assert await code_exec._execute_code(None, tmp_path, {"code": "pass"}, tool_name="execute_code_e2b") == (
        "❌ E2B sandbox configuration error: missing key\nPlease check the API key in the tool settings."
    )
    assert fallback_calls == [
        ((tmp_path, {"code": "pass"}), {"allow_network": True, "max_timeout": 7, "on_output": None})
    ]


async def test_execute_code_preserves_execution_failure_fallback(monkeypatch, tmp_path):
    config = SandboxConfig(allow_network=False, max_timeout=6)
    backend = FakeBackend(RuntimeError("backend boom"))
    fallback_calls = []

    async def get_tool_config(_agent_id, _tool_name):
        return None

    async def legacy(*args, **kwargs):
        fallback_calls.append((args, kwargs))
        return "legacy result"

    monkeypatch.setattr("app.config.get_sandbox_config", lambda: config)
    monkeypatch.setattr("app.services.sandbox.registry.get_sandbox_backend", lambda _config: backend)
    monkeypatch.setattr(agent_tools, "_get_tool_config", get_tool_config)
    monkeypatch.setattr(agent_tools, "_execute_code_legacy", legacy)

    assert await code_exec._execute_code(None, tmp_path, {"code": "pass"}) == "legacy result"
    assert fallback_calls == [
        ((tmp_path, {"code": "pass"}), {"allow_network": False, "max_timeout": 6, "on_output": None})
    ]


async def test_legacy_execution_uses_facade_safety_and_runtime_capture_limits(monkeypatch, tmp_path):
    process = FakeProcess([b"abcdef"], [b"xyz"])
    subprocess_calls = []
    output_calls = []

    def facade_safety(_language, _code, _allow_network):
        return None

    async def create_process(*command, **kwargs):
        subprocess_calls.append((command, kwargs))
        return process

    async def on_output(text, label):
        output_calls.append((text, label))

    monkeypatch.setattr(agent_tools, "_check_code_safety", facade_safety)
    monkeypatch.setattr(agent_tools, "MAX_EXEC_STDOUT_CAPTURE_BYTES", 3)
    monkeypatch.setattr(agent_tools, "MAX_EXEC_STDERR_CAPTURE_BYTES", 2)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    result = await code_exec._execute_code_legacy(tmp_path, {"code": "print('ok')"}, on_output=on_output)

    script_path = tmp_path / "_exec_tmp.py"
    assert result == "📤 Output:\nabc\n\n⚠️ Stderr:\nxy"
    assert process.stdout.read_sizes == [4096, 4096]
    assert process.stderr.read_sizes == [4096, 4096]
    assert output_calls == [("abcdef", "stdout"), ("xyz", "stderr")]
    assert not script_path.exists()
    assert subprocess_calls[0][0] == ("python3", str(script_path))
    assert subprocess_calls[0][1]["cwd"] == str(tmp_path.resolve())
    assert subprocess_calls[0][1]["env"]["HOME"] == str(tmp_path.resolve())
    assert subprocess_calls[0][1]["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


async def test_legacy_execution_handles_timeout_and_process_creation_error(monkeypatch, tmp_path):
    process = FakeProcess([], [])

    async def create_process(*_args, **_kwargs):
        return process

    async def timeout_wait(awaitable, **_kwargs):
        await awaitable
        raise TimeoutError

    monkeypatch.setattr(agent_tools, "_check_code_safety", lambda *_args: None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(asyncio, "wait_for", timeout_wait)

    result = await code_exec._execute_code_legacy(tmp_path, {"code": "pass", "timeout": 2})

    assert process.killed is True
    assert result.endswith("higher 'timeout' parameter (up to 3600s).")
    assert not (tmp_path / "_exec_tmp.py").exists()

    async def create_failure(*_args, **_kwargs):
        raise RuntimeError("launch failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_failure)
    assert await code_exec._execute_code_legacy(tmp_path, {"code": "pass"}) == "❌ Execution error: launch failed"
    assert not (tmp_path / "_exec_tmp.py").exists()
