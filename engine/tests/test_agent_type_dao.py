import re
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from app.core.domain.agents import AgentType
from app.dao.agent_dao import agent_dao
from app.dao.agent_setup_dao import agent_setup_dao
from app.dao.agent_setup_mcp_dao import agent_setup_mcp_dao
from app.records.agent_setup_mcp import AgentSetupMCPWork

PLACEHOLDER = re.compile(r"%\((\w+)\)s")
AGENT_TYPE_PREDICATE = re.compile(r"agent_type\s*=")


@dataclass
class RecordingDb:
    calls: list[tuple[str, Mapping[str, object]]] = field(default_factory=list)

    async def fetchone(self, query: str, params: Mapping[str, object]) -> None:
        self.calls.append((query, params))

    async def fetchall(self, query: str, params: Mapping[str, object]) -> list[dict[str, object]]:
        self.calls.append((query, params))
        return []

    async def fetchval(self, query: str, params: Mapping[str, object]) -> None:
        self.calls.append((query, params))

    async def execute(self, query: str, params: Mapping[str, object]) -> None:
        self.calls.append((query, params))

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[RecordingDb]:
        yield self


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> RecordingDb:
    db = RecordingDb()
    monkeypatch.setattr(agent_dao, "session", db.session)
    monkeypatch.setattr(agent_setup_dao, "session", db.session)
    monkeypatch.setattr("app.dao.agent_setup_mcp_dao.connection_ctx", db.session)
    return db


def assert_agent_type_bound(db: RecordingDb) -> None:
    eligibility = [(query, params) for query, params in db.calls if AGENT_TYPE_PREDICATE.search(query)]
    assert eligibility
    for query, params in eligibility:
        # The predicate is a bound wire value, never a SQL literal, and every placeholder has an argument.
        assert "'openclaw'" not in query
        assert params["agent_type"] == "openclaw"
        assert type(params["agent_type"]) is str
        assert set(PLACEHOLDER.findall(query)) <= params.keys()


async def test_list_by_names_binds_enum_wire_value(db: RecordingDb) -> None:
    # Given an optional runtime type filter.
    # When listing tenant agents by name.
    _ = await agent_dao.list_by_names_for_tenant(uuid4(), ["Morty"], agent_type=AgentType.OPENCLAW)
    # Then the SQL receives the text value.
    assert_agent_type_bound(db)


async def test_list_by_names_without_type_adds_no_predicate(db: RecordingDb) -> None:
    _ = await agent_dao.list_by_names_for_tenant(uuid4(), ["Morty"])
    assert all(not AGENT_TYPE_PREDICATE.search(query) for query, _params in db.calls)


async def test_setup_retry_predicates_bind_openclaw(db: RecordingDb) -> None:
    # Given both setup eligibility queries that retain the OpenClaw-only rule.
    agent_id = uuid4()
    # When reading status and retrying.
    _ = await agent_setup_dao.setup_status(agent_id)
    _ = await agent_setup_dao.retry_failed(agent_id)
    # Then each predicate is parameterized without losing its id argument.
    assert_agent_type_bound(db)
    assert sum(bool(AGENT_TYPE_PREDICATE.search(query)) for query, _params in db.calls) == 2


async def test_setup_mcp_eligibility_binds_openclaw_in_every_query(db: RecordingDb) -> None:
    # Given a claimed MCP work row.
    work = AgentSetupMCPWork(
        agent_id=uuid4(),
        server_id="vendor/qa",
        owner_id=uuid4(),
        tenant_id=None,
        requester_id=uuid4(),
        state="running",
        warning_code=None,
        attempts=1,
        claim_token=uuid4(),
        lease_until=None,
    )
    # When every operation that embeds the shared eligibility predicate runs.
    _ = await agent_setup_mcp_dao.statuses(work.agent_id)
    _ = await agent_setup_mcp_dao.eligible(work)
    _ = await agent_setup_mcp_dao.claim()
    _ = await agent_setup_mcp_dao.finish(work, None)
    # Then all four carry the bound type and the rest of their parameter maps.
    assert_agent_type_bound(db)
    assert sum(bool(AGENT_TYPE_PREDICATE.search(query)) for query, _params in db.calls) == 4
