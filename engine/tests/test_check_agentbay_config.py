from __future__ import annotations

import importlib.util
import inspect
import uuid
from pathlib import Path

import pytest

from app.services.agentbay_config import AgentBayConfigResolution, AgentBayConfigSource

_checker_spec = importlib.util.spec_from_file_location(
    "check_agentbay_config", Path(__file__).parent.parent / "check_agentbay_config.py"
)
assert _checker_spec is not None
assert _checker_spec.loader is not None
checker = importlib.util.module_from_spec(_checker_spec)
_checker_spec.loader.exec_module(checker)


async def test_run_returns_two_without_resolver_when_agent_uuid_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given: a resolver that records whether the CLI reaches the DB-owning seam.
    resolver_calls: list[tuple[uuid.UUID | None, None]] = []

    async def resolve_config(agent_id: uuid.UUID | None, db: None) -> AgentBayConfigResolution:
        resolver_calls.append((agent_id, db))
        return AgentBayConfigResolution(AgentBayConfigSource.UNRESOLVED, None)

    monkeypatch.setattr(checker, "resolve_agentbay_config", resolve_config)

    # When: the operator supplies a malformed agent UUID.
    exit_code = await checker.run(("not-a-uuid",))

    # Then: parsing fails before the resolver can open a database session.
    captured = capsys.readouterr()
    assert exit_code == 2
    assert resolver_calls == []
    assert "Invalid agent UUID" in captured.err
    assert captured.out == ""


async def test_run_uses_global_resolution_without_agent_uuid(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given: a usable global configuration with a known secret.
    resolver_calls: list[tuple[uuid.UUID | None, None]] = []
    secret = "akm-global-secret-must-not-be-printed"

    async def resolve_config(agent_id: uuid.UUID | None, db: None) -> AgentBayConfigResolution:
        resolver_calls.append((agent_id, db))
        return AgentBayConfigResolution(AgentBayConfigSource.BROWSER_NAVIGATE_TOOL, secret)

    monkeypatch.setattr(checker, "resolve_agentbay_config", resolve_config)

    # When: the operator does not select an agent.
    exit_code = await checker.run(())

    # Then: the resolver receives the global lookup and only its source is reported.
    captured = capsys.readouterr()
    assert exit_code == 0
    assert resolver_calls == [(None, None)]
    assert captured.out == "AgentBay configuration source: browser_navigate_tool\n"
    assert secret not in captured.out
    assert captured.err == ""


async def test_run_passes_uuid_to_agent_first_resolver(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given: a selected agent and a usable per-agent configuration.
    agent_id = uuid.uuid4()
    resolver_calls: list[tuple[uuid.UUID | None, None]] = []
    secret = "akm-agent-secret-must-not-be-printed"

    async def resolve_config(received_agent_id: uuid.UUID | None, db: None) -> AgentBayConfigResolution:
        resolver_calls.append((received_agent_id, db))
        return AgentBayConfigResolution(AgentBayConfigSource.PER_AGENT_CHANNEL, secret)

    monkeypatch.setattr(checker, "resolve_agentbay_config", resolve_config)

    # When: the operator selects the agent UUID.
    exit_code = await checker.run((str(agent_id),))

    # Then: the resolver performs the agent-first lookup and the secret is not reported.
    captured = capsys.readouterr()
    assert exit_code == 0
    assert resolver_calls == [(agent_id, None)]
    assert captured.out == "AgentBay configuration source: per_agent_channel\n"
    assert secret not in captured.out
    assert captured.err == ""


async def test_run_returns_one_when_resolver_cannot_select_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given: an unresolved configuration from the shared resolver.
    async def resolve_config(agent_id: uuid.UUID | None, db: None) -> AgentBayConfigResolution:
        return AgentBayConfigResolution(AgentBayConfigSource.UNRESOLVED, None)

    monkeypatch.setattr(checker, "resolve_agentbay_config", resolve_config)

    # When: the operator requests a global lookup.
    exit_code = await checker.run(())

    # Then: the CLI reports the selected source and signals that no usable key exists.
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == "AgentBay configuration source: unresolved\n"
    assert captured.err == ""


def test_source_reporter_cannot_receive_a_secret() -> None:
    # Given: the CLI's source-reporting boundary.
    signature = inspect.signature(checker._report_source)
    annotations = inspect.get_annotations(checker._report_source, eval_str=True)

    # When: its public input shape is inspected.
    parameters = tuple(signature.parameters.values())

    # Then: formatting receives only the source enum, never a resolution or key.
    assert len(parameters) == 1
    assert parameters[0].name == "source"
    assert annotations == {"source": AgentBayConfigSource, "return": None}
