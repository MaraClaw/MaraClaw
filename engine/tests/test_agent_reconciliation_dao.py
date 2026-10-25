from collections.abc import AsyncGenerator, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from app.core.row_memo import clear_row_memo, memo_get, memo_set
from app.dao.agent_dao import agent_dao
from app.records.agent import AgentRecord
from app.services import agent_stops

NOW = datetime(2026, 10, 22, tzinfo=UTC)


@dataclass
class RuntimeRows:
    row: dict[str, object] | None = field(
        default_factory=lambda: {
            "id": uuid4(),
            "creator_id": uuid4(),
            "name": "A",
            "status": "stopped",
            "container_id": "missing",
            "container_port": 3210,
        }
    )

    async def fetchone(self, query: str, params: Mapping[str, object]) -> dict[str, object] | None:
        if query.startswith("SELECT"):
            return dict(self.row) if self.row is not None else None
        assert "SET container_id = NULL, container_port = NULL" in query
        assert "WHERE id = %(id)s AND container_id = %(container_id)s AND status = 'stopped'" in query
        if self.row is None or any(self.row[key] != params[key] for key in ("id", "container_id")):
            return None
        if self.row["status"] != "stopped":
            return None
        self.row.update(container_id=None, container_port=None)
        return {"id": self.row["id"]}

    async def fetchall(self, query: str, params: Mapping[str, object] | None = None) -> list[dict[str, object]]:
        if query.startswith(("SELECT tenant_id", "UPDATE")):
            return []
        assert "WHERE status = 'stopped' AND container_id IS NOT NULL" in query
        if self.row is not None and self.row["status"] == "stopped" and self.row["container_id"] is not None:
            return [dict(self.row)]
        return []

    async def execute(self, query: str, params: Mapping[str, object]) -> None:
        assert query.startswith("UPDATE agent_start_operations o SET state='cancelled',updated_at=now() FROM agents a ")
        assert "WHERE o.agent_id=a.id AND o.state='pending' AND (a.is_expired OR a.expires_at <= %(now)s)" in query
        assert params == {"now": NOW}

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[RuntimeRows]:
        yield self


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> Iterator[RuntimeRows]:
    clear_row_memo()
    rows = RuntimeRows()
    monkeypatch.setattr(agent_dao, "session", rows.session)
    yield rows
    clear_row_memo()


async def test_missing_stopped_reference_cas_clears_id_port_and_cached_row(rows: RuntimeRows) -> None:
    # Given the same missing guest and stopped intent in the database and row memo.
    assert rows.row is not None
    agent = AgentRecord.from_row(rows.row)
    memo_set("agent", agent.id, agent)
    # When the missing-reference CAS succeeds.
    changed = await agent_dao.clear_missing_stopped_container(agent.id, "missing")
    # Then persisted runtime coordinates and the stale cached row are cleared together.
    assert changed is True
    assert (rows.row["container_id"], rows.row["container_port"]) == (None, None)
    assert memo_get("agent", agent.id) is None


@pytest.mark.parametrize("superseded", ["reference", "intent", "both", "agent", "deleted"])
async def test_missing_reference_cas_never_clobbers_superseding_state(rows: RuntimeRows, superseded: str) -> None:
    # Given the old missing-reference proof and a concurrently superseded database row.
    assert rows.row is not None
    agent = AgentRecord.from_row(rows.row)
    if superseded in {"reference", "both"}:
        rows.row.update(container_id="new", container_port=4321)
    if superseded in {"intent", "both"}:
        rows.row["status"] = "running"
    if superseded == "agent":
        rows.row["id"] = uuid4()
    if superseded == "deleted":
        rows.row = None
    expected = dict(rows.row) if rows.row is not None else None
    # When the old missing-reference proof tries to clear runtime coordinates.
    changed = await agent_dao.clear_missing_stopped_container(agent.id, "missing")
    # Then a mismatched agent/reference/intent cannot erase newer state.
    assert changed is False
    assert rows.row == expected


async def test_missing_reference_reconciliation_converges_without_repeating_stops(
    rows: RuntimeRows, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given a missing current guest and a stale memo from before a different publication.
    assert rows.row is not None
    agent = AgentRecord.from_row(rows.row)
    agent.container_id = "stale"
    memo_set("agent", agent.id, agent)
    stopped: list[str | None] = []

    async def missing(current: AgentRecord) -> bool:
        stopped.append(current.container_id)
        current.container_id = None
        return True

    def agent_dir(agent_id: UUID) -> Path:
        return tmp_path / str(agent_id)

    monkeypatch.setattr(agent_stops.agent_manager, "_agent_dir", agent_dir)
    monkeypatch.setattr(agent_stops.agent_manager, "stop_container", missing)
    monkeypatch.setattr(agent_stops, "sweep_orphan_containers", AsyncMock())
    monkeypatch.setattr(agent_stops, "reconcile_deletion_cleanup", AsyncMock())
    monkeypatch.setattr("app.services.company_transfer_cleanup.reconcile_transfer_cleanup", AsyncMock())
    # When repeated reconciliation ticks process the same database.
    for _ in range(2):
        await agent_stops.reconcile_stopped_containers(NOW)
    # Then the fresh missing reference is cleared and never retried on the second tick.
    assert stopped == ["missing"]
    assert (rows.row["container_id"], rows.row["container_port"]) == (None, None)
