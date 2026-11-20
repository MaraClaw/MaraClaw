"""Live PostgreSQL + loopback S3 world for the workspace mirror tests (``MARACLAW_TEST_POSTGRES_DSN``)."""

import hashlib
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from app.dao.agent_dao import agent_dao
from app.dao.workspace_mirror_entry_dao import workspace_mirror_entry_dao
from app.records.agent import AgentRecord
from app.records.workspace_mirror_entry import WorkspaceMirrorEntryRecord
from app.services import workspace_mirror
from app.services.workspace_mirror import MirrorMode, ReconcileReport, reconcile_agent
from managed_storage_fixtures import StorageWorld

SECOND_NS = 1_000_000_000
AGED_NS = 60 * SECOND_NS
HOUR_NS = 3600 * SECOND_NS


@dataclass
class Clock:
    now_ns: int

    def __call__(self) -> int:
        return self.now_ns


@dataclass
class Mirror:
    world: StorageWorld
    agent_id: UUID
    agent_dir: Path
    clock: Clock

    def key(self, rel: str) -> str:
        return f"{self.agent_id}/{rel}"

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock.now_ns / SECOND_NS, UTC)

    def write(self, rel: str, data: bytes, *, age_ns: int = AGED_NS) -> Path:
        """A guest write whose mtime sits `age_ns` before the mirror's clock."""
        path = self.agent_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(data)
        os.utime(path, ns=(self.clock.now_ns - age_ns, self.clock.now_ns - age_ns))
        return path

    def read(self, rel: str) -> bytes | None:
        path = self.agent_dir / rel
        return path.read_bytes() if path.exists() else None

    def stored(self, rel: str) -> bytes | None:
        return self.world.stored(self.key(rel))

    async def put(self, rel: str, data: bytes) -> None:
        await self.world.backend.write_bytes(self.key(rel), data)

    async def run(
        self, mode: MirrorMode = MirrorMode.FULL, *, budget_seconds: float = workspace_mirror.PASS_BUDGET_SECONDS
    ) -> ReconcileReport:
        return await reconcile_agent(
            self.agent_id, self.agent_dir, self.world.backend, mode=mode, budget_seconds=budget_seconds
        )

    async def entries(self) -> dict[str, WorkspaceMirrorEntryRecord]:
        return await workspace_mirror_entry_dao.list_for_agent(self.agent_id)

    async def agent(self) -> AgentRecord:
        record = await agent_dao.get(self.agent_id)
        assert record is not None
        return record

    def store_writes(self) -> int:
        state = self.world.s3.state
        return state.count("PUT") + state.count("DELETE") + state.count("MULTIDELETE")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
async def mirror(quota_world: StorageWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Mirror:
    agent_id = await quota_world.agent()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    # Ten minutes ahead: every write a test makes has settled, while the hour-long too-large back-off still holds.
    clock = Clock(time.time_ns() + 600 * SECOND_NS)
    monkeypatch.setattr(workspace_mirror, "_wall_clock_ns", clock)
    monkeypatch.setattr(workspace_mirror, "durable_store", lambda: quota_world.backend)
    monkeypatch.setattr(workspace_mirror, "agent_state_dir", lambda agent: tmp_path / str(agent))
    return Mirror(quota_world, agent_id, agent_dir, clock)
