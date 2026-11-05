import asyncio
import errno
import json
import os
from contextlib import asynccontextmanager
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pywhales.exceptions import DockerException

from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services import openclaw_gateway_config, openclaw_gateway_rpc as rpc
from app.services.agent_manager import agent_manager
from app.services.guest_identity import GuestOwnershipError


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
    monkeypatch.setattr(agent_manager, "docker_client", SimpleNamespace())
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
    monkeypatch.setattr(agent_manager, "docker_client", SimpleNamespace())
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
    monkeypatch.setattr(agent_manager, "docker_client", SimpleNamespace())
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
    monkeypatch.setattr(agent_manager, "docker_client", SimpleNamespace())
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
