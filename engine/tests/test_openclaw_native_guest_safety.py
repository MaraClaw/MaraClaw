import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pywhales.exceptions import DockerException

from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services import openclaw_gateway_config, openclaw_gateway_rpc as rpc
from app.services.agent_manager import agent_manager
from guest_bootstrap_fakes import BOOTSTRAP_PENDING_EXIT, PENDING_STDERR
from test_openclaw_gateway_rpc import running_client


@pytest.mark.parametrize(
    ("code", "stderr", "refused"),
    [
        (BOOTSTRAP_PENDING_EXIT, PENDING_STDERR, True),
        (BOOTSTRAP_PENDING_EXIT, None, False),
        (BOOTSTRAP_PENDING_EXIT, "other failure", False),
        (BOOTSTRAP_PENDING_EXIT, f"warning\n{PENDING_STDERR}", False),
        (1, PENDING_STDERR, False),
    ],
)
async def test_only_exact_guard_refusal_is_proven_not_submitted(monkeypatch, tmp_path, code, stderr, refused):
    agent = AgentRecord(id=uuid4(), name="guest", creator_id=uuid4())
    message = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="hello")

    @asynccontextmanager
    async def exclusion(_agent_id):
        yield

    async def allowed(_agent_id):
        return None

    async def fresh(_agent_id):
        return agent

    def execute(*_args):
        raise DockerException(["docker", "exec"], code, stderr=None if stderr is None else stderr.encode())

    monkeypatch.setattr(rpc, "guest_write_section", exclusion)
    monkeypatch.setattr(rpc, "assert_work_allowed", allowed)
    monkeypatch.setattr(rpc.agent_dao, "get", fresh)
    monkeypatch.setattr(rpc, "resolve_guest", lambda *_args: SimpleNamespace(id="owned"))
    monkeypatch.setattr(rpc, "_execute", execute)
    monkeypatch.setattr(agent_manager, "docker_client", running_client())
    monkeypatch.setattr(agent_manager, "_agent_dir", lambda _id: tmp_path)
    monkeypatch.setattr(openclaw_gateway_config, "prepare_native_gateway_files", lambda _directory: None)

    outcome = await rpc.execute_gateway_turn(agent, message)

    if refused:
        assert outcome == rpc.NotAdmittedOutcome(str(message.id), "not_submitted", "bootstrap_pending")
    else:
        assert outcome == rpc.UncertainOutcome(str(message.id), "unknown", None, "exec_failed")
    assert not list(tmp_path.glob(".maraclaw-rpc-*.json"))


async def test_each_native_turn_has_private_isolated_payload_and_cleans_up(monkeypatch, tmp_path):
    agent = AgentRecord(id=uuid4(), name="guest", creator_id=uuid4())
    messages = [GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content=content) for content in ("first", "second")]
    observed = []

    @asynccontextmanager
    async def exclusion(_agent_id):
        yield

    async def allowed(_agent_id):
        return None

    async def fresh(_agent_id):
        return agent

    def execute(_container, payload_name, _cancel):
        path = Path(tmp_path) / payload_name
        observed.append((payload_name, path.stat().st_mode & 0o777, json.loads(path.read_text())))
        return "{}"

    monkeypatch.setattr(rpc, "guest_write_section", exclusion)
    monkeypatch.setattr(rpc, "assert_work_allowed", allowed)
    monkeypatch.setattr(rpc.agent_dao, "get", fresh)
    monkeypatch.setattr(rpc, "resolve_guest", lambda *_args: SimpleNamespace(id="owned"))
    monkeypatch.setattr(rpc, "_execute", execute)
    monkeypatch.setattr(agent_manager, "docker_client", running_client())
    monkeypatch.setattr(agent_manager, "_agent_dir", lambda _id: tmp_path)
    monkeypatch.setattr(openclaw_gateway_config, "prepare_native_gateway_files", lambda _directory: None)

    for message in messages:
        outcome = await rpc.execute_gateway_turn(agent, message)
        assert outcome == rpc.UncertainOutcome(str(message.id), "unknown", None, "invalid_output")

    assert observed[0][0] != observed[1][0]
    assert [item[1] for item in observed] == [0o600, 0o600]
    assert [(item[2]["messageId"], item[2]["message"]) for item in observed] == [
        (str(message.id), message.content) for message in messages
    ]
    assert not list(tmp_path.glob(".maraclaw-rpc-*.json"))
