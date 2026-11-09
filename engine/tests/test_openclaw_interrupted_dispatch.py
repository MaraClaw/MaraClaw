"""An interrupted, never-consumed native turn must not block the next message in its lane forever.

Real ``OpenClawDispatchReconciler.scan_once`` over a small real SQLite guest fixture (the installed
``session_pending_inputs`` shape) and an in-memory gateway lane that honours the pending-head SQL rules.
Settlement goes through the real ``settle_native_report`` / ``record_report`` boundary.
"""

import json
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services import (
    openclaw_dispatch_reconciler as module,
    openclaw_gateway_delivery as delivery,
    openclaw_routing as routing,
)
from app.services.agent_manager import agent_manager

LANE = "ordinary-thread"
T0 = datetime(2026, 11, 7, 3, 0, tzinfo=UTC)


def _session_key(binding: UUID) -> str:
    return f"agent:main:hook:maraclaw:{binding}"


@dataclass(slots=True)
class Lane:
    """Gateway rows of one conversation lane plus the observations the assertions read."""

    predecessor: GatewayMessageRecord
    successor: GatewayMessageRecord
    rows: dict[UUID, GatewayMessageRecord] = field(default_factory=dict)
    statuses: list[str] = field(default_factory=list)
    wakes: list[tuple[str, list[UUID]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.rows = {row.id: row for row in (self.predecessor, self.successor)}
        self.statuses = [self.predecessor.status]

    def pending_heads(self) -> list[UUID]:
        """Mirror of ``pending_dispatch_page``: a pending row is a head only when nothing earlier holds the lane."""
        rows = sorted(self.rows.values(), key=lambda r: (r.created_at, r.id))
        if any(row.status == "delivered" for row in rows):
            return []
        return [row.id for row in rows if row.status == "pending"][:1]

    async def pending_dispatch_page(
        self, limit: int, *, after: tuple[datetime, UUID] | None = None
    ) -> tuple[list[GatewayMessageRecord], None]:
        assert after is None
        return [self.rows[key] for key in self.pending_heads()][:limit], None

    async def delivered_dispatch_page(
        self, limit: int, *, after: tuple[datetime, UUID] | None = None
    ) -> tuple[list[GatewayMessageRecord], None]:
        """Stand-in for whichever delivered-lane read the scanner gains; same shape as the pending page."""
        assert after is None
        return [row for row in self.rows.values() if row.status == "delivered"][:limit], None

    async def get_for_agent(self, message_id: UUID, agent_id: UUID) -> GatewayMessageRecord | None:
        row = self.rows.get(message_id)
        return row if row is not None and row.agent_id == agent_id else None

    async def complete(
        self, message_id: UUID, agent_id: UUID, *, result: str, now: datetime, binding_id: UUID | None = None
    ) -> GatewayMessageRecord | None:
        row = await self.get_for_agent(message_id, agent_id)
        if row is None or row.status not in ("pending", "delivered"):
            return None
        if binding_id is not None and row.reply_binding_id != binding_id:
            return None
        done = replace(row, status="completed", result=result, completed_at=now)
        self.rows[message_id] = done
        if message_id == self.predecessor.id:
            self.statuses.append("completed")
        return done

    def wake(self, _agent: AgentRecord, _message_id: UUID) -> None:
        self.wakes.append((self.rows[self.predecessor.id].status, self.pending_heads()))


@pytest.fixture
def lane(monkeypatch: pytest.MonkeyPatch) -> Lane:
    agent = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4())
    binding = uuid4()
    predecessor = GatewayMessageRecord(
        id=uuid4(),
        agent_id=agent.id,
        content="first",
        status="delivered",
        conversation_id=LANE,
        created_at=T0,
        delivered_at=T0 + timedelta(seconds=1),
        reply_binding_id=binding,
    )
    successor = GatewayMessageRecord(
        id=uuid4(),
        agent_id=agent.id,
        content="second",
        conversation_id=LANE,
        created_at=T0 + timedelta(seconds=30),
        reply_binding_id=binding,
    )
    value = Lane(predecessor, successor)

    @asynccontextmanager
    async def connection():
        yield None

    gateway = delivery.gateway_message_dao
    monkeypatch.setattr(gateway, "pending_dispatch_page", value.pending_dispatch_page)
    monkeypatch.setattr(gateway, "delivered_dispatch_page", value.delivered_dispatch_page, raising=False)
    monkeypatch.setattr(gateway, "get_for_agent", value.get_for_agent)
    monkeypatch.setattr(gateway, "complete", value.complete)
    monkeypatch.setattr(delivery, "connection_ctx", connection)
    for name in ("lock_tenants", "lock_user", "lock_agent"):
        monkeypatch.setattr(delivery, name, AsyncMock())
    monkeypatch.setattr(delivery.agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr(delivery.agent_dao, "get_many", AsyncMock(return_value=[agent]))
    monkeypatch.setattr(delivery.agent_dao, "update", AsyncMock(return_value=agent))
    monkeypatch.setattr(delivery, "publish_report", AsyncMock())
    monkeypatch.setattr(delivery, "response_guard", AsyncMock(return_value=SimpleNamespace(redact=lambda text: text)))
    monkeypatch.setattr(delivery.participant_dao, "get_by_type_ref", AsyncMock(return_value=None))
    monkeypatch.setattr(
        delivery.chat_message_dao, "insert_message", AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    )
    monkeypatch.setattr(module, "wake_shared_lane", value.wake)
    monkeypatch.setattr(routing, "wake_shared_lane", value.wake)
    return value


@dataclass(frozen=True, slots=True)
class Evidence:
    """What the guest holds for the predecessor; ``state=None`` writes no pending-input row."""

    state: str | None = "interrupted"
    consumed_event_id: str | None = None
    foreign_session: bool = False
    foreign_run: bool = False
    tombstone: bool = True
    recovery_active: bool = False


def _entry(message: GatewayMessageRecord, evidence: Evidence) -> str:
    """``session_nodes.entry_json`` after startup recovery: the original's terminal tombstone, or a live recovery."""
    original, recovery = str(message.id), str(uuid4())
    entry: dict[str, object] = {
        "status": "running" if evidence.recovery_active else "done",
        "restartRecoveryDeliverySourceRunId": original if evidence.recovery_active else None,
        "restartRecoveryDeliveryRunId": recovery if evidence.recovery_active else None,
    }
    if evidence.tombstone:
        entry["restartRecoveryTerminalRunIds"] = [original, recovery]
        entry["restartRecoveryTerminalDeliveryEvidence"] = [
            {"runId": original, "captured": True, "transcriptRunId": recovery}
        ]
    return json.dumps(entry)


def guest(root: Path, message: GatewayMessageRecord, evidence: Evidence) -> None:
    """Write the guest database as OpenClaw 2026.11.2 leaves it."""
    assert message.reply_binding_id is not None
    key = _session_key(uuid4() if evidence.foreign_session else message.reply_binding_id)
    path = root / "agents/main/agent/openclaw-agent.sqlite"
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "CREATE TABLE session_nodes (session_key TEXT NOT NULL PRIMARY KEY, current_session_id TEXT NOT NULL, "
            "entry_json TEXT NOT NULL, updated_at INTEGER NOT NULL, status TEXT) STRICT"
        )
        db.execute(
            "CREATE TABLE session_windows (session_id TEXT NOT NULL PRIMARY KEY, session_key TEXT NOT NULL, "
            "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
            "FOREIGN KEY (session_key) REFERENCES session_nodes(session_key) ON DELETE CASCADE) STRICT"
        )
        db.execute(
            "CREATE TABLE session_pending_inputs ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, input_id TEXT NOT NULL UNIQUE, session_key TEXT NOT NULL, "
            "session_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, run_id TEXT NOT NULL, "
            "request_hash TEXT NOT NULL, message_json TEXT NOT NULL, lifecycle_generation TEXT NOT NULL, "
            "state TEXT NOT NULL CHECK (state IN ('queued', 'interrupted', 'cancelled')), "
            "accepted_at INTEGER NOT NULL, consumed_event_id TEXT, UNIQUE (session_id, idempotency_key), "
            "FOREIGN KEY (session_key) REFERENCES session_nodes(session_key) ON DELETE CASCADE, "
            "FOREIGN KEY (session_id) REFERENCES session_windows(session_id) ON DELETE CASCADE) STRICT"
        )
        db.execute(
            "INSERT INTO session_nodes VALUES (?, 'window', ?, 1, ?)",
            (key, _entry(message, evidence), "running" if evidence.recovery_active else "done"),
        )
        db.execute("INSERT INTO session_windows VALUES ('window', ?, 1, 1)", (key,))
        if evidence.state is not None:
            db.execute(
                "INSERT INTO session_pending_inputs (input_id, session_key, session_id, idempotency_key, run_id, "
                "request_hash, message_json, lifecycle_generation, state, accepted_at, consumed_event_id) "
                "VALUES (?, ?, 'window', ?, ?, 'hash', '{}', 'generation', ?, 1, ?)",
                (
                    str(uuid4()),
                    key,
                    f"{message.id}:user",
                    str(uuid4() if evidence.foreign_run else message.id),
                    evidence.state,
                    evidence.consumed_event_id,
                ),
            )
        db.commit()
    finally:
        db.close()


@pytest.fixture
def guest_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(agent_manager, "_agent_dir", lambda _agent_id: tmp_path)
    return tmp_path


async def test_interrupted_unconsumed_predecessor_is_settled_and_successor_woken(lane: Lane, guest_root: Path) -> None:
    guest(guest_root, lane.predecessor, Evidence())
    scanner = module.OpenClawDispatchReconciler()
    assert lane.pending_heads() == []
    _ = await scanner.scan_once()
    assert lane.rows[lane.predecessor.id].status == "completed"
    assert bool(lane.rows[lane.predecessor.id].result)
    assert lane.rows[lane.successor.id].status == "pending"
    assert ("completed", [lane.successor.id]) in lane.wakes
    assert lane.statuses == ["delivered", "completed"]


@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param(Evidence(state=None), id="absent"),
        pytest.param(Evidence(state="queued"), id="queued"),
        pytest.param(Evidence(consumed_event_id="event-1"), id="consumed"),
        pytest.param(Evidence(foreign_session=True), id="wrong-session"),
        pytest.param(Evidence(foreign_run=True), id="other-run"),
        pytest.param(Evidence(tombstone=False), id="missing-tombstone"),
        pytest.param(Evidence(recovery_active=True), id="recovery-active"),
        pytest.param(Evidence(tombstone=False, recovery_active=True), id="recovery-active-untombstoned"),
    ],
)
async def test_uncertain_evidence_leaves_predecessor_delivered(
    lane: Lane, guest_root: Path, evidence: Evidence
) -> None:
    guest(guest_root, lane.predecessor, evidence)
    _ = await module.OpenClawDispatchReconciler().scan_once()
    assert lane.rows[lane.predecessor.id].status == "delivered"
    assert lane.statuses == ["delivered"]
    assert lane.wakes == []
