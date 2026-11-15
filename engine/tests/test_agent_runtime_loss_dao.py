"""Execute the portable loss SQL in memory; live PostgreSQL locking is not covered here."""

import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import override
from uuid import uuid4

import pytest

from app.dao.agent_runtime_loss_dao import agent_runtime_loss_dao as dao
from app.db.connection import DbConnection, Params, Row


class LossDatabase(DbConnection):
    def __init__(self):
        self.sqlite = sqlite3.connect(":memory:")
        self.sqlite.row_factory = sqlite3.Row
        self.statements: list[str] = []
        self.now = "2026-11-15T00:00:00+00:00"
        self.sqlite.create_function("now", 0, lambda: self.now)
        self.sqlite.execute(
            "CREATE TABLE agent_runtime_losses (agent_id TEXT PRIMARY KEY, reason TEXT NOT NULL, "
            "recorded_container_id TEXT, image TEXT, detected_at TEXT NOT NULL DEFAULT (now()), notified_at TEXT)"
        )

    @override
    async def fetchone(self, query: str, params: Params = None) -> Row | None:
        self.statements.append(query)
        assert isinstance(params, Mapping)
        sql = re.sub(r"%\((\w+)\)s", r":\1", query)
        values = {key: str(value) if value is not None else None for key, value in params.items()}
        row = self.sqlite.execute(sql, values).fetchone()
        if row is None:
            return None
        columns = row.keys()
        result: Row = {str(key): row[key] for key in columns}
        detected_at = result.get("detected_at")
        if isinstance(detected_at, str):
            result["detected_at"] = datetime.fromisoformat(detected_at)
        return result

    @override
    async def execute(self, query: str, params: Params = None) -> None:
        _ = await self.fetchone(query, params)


@pytest.fixture
def db():
    database = LossDatabase()
    try:
        yield database
    finally:
        database.sqlite.close()


@pytest.mark.parametrize("first_reason", ["container_missing", "image_unavailable", "container_stopped"])
@pytest.mark.parametrize("next_reason", ["container_missing", "image_unavailable", "container_stopped"])
async def test_reason_transitions_reset_detection_and_notification_only_when_changed(db, first_reason, next_reason):
    # Given an announced loss and a later report, including image enrichment and reason changes.
    agent_id = uuid4()
    first = await dao.record(db, agent_id, first_reason, recorded_container_id="original", image="old-image")
    assert await dao.claim_notification(db, agent_id)
    db.now = "2026-11-15T01:00:00+00:00"
    # When the same agent is reported again.
    latest = await dao.record(db, agent_id, next_reason, recorded_container_id="later", image=None)
    # Then another reason starts a new alertable episode; repeats preserve detection, notification and image.
    changed = next_reason != first_reason
    assert latest.reason == next_reason
    assert latest.detected_at == (datetime.fromisoformat(db.now) if changed else first.detected_at)
    assert latest.image == (None if changed else "old-image")
    assert latest.recorded_container_id == "original"
    assert await dao.claim_notification(db, agent_id) is changed
    assert not await dao.claim_notification(db, agent_id)


@pytest.mark.parametrize(
    "concurrent_reason", [None, "container_missing", "image_unavailable", "container_stopped", "absent"]
)
async def test_running_clear_preserves_a_newer_different_reason_and_other_agent(db, concurrent_reason):
    # Given a stopped loss observed before a concurrent report replaces it.
    agent_id, other_id = uuid4(), uuid4()
    for identity in (agent_id, other_id):
        if identity != agent_id or concurrent_reason != "absent":
            _ = await dao.record(db, identity, "container_stopped", recorded_container_id="owned", image=None)
    if concurrent_reason not in (None, "absent"):
        _ = await dao.record(db, agent_id, concurrent_reason, recorded_container_id="owned", image=None)
    # When the earlier running observation clears stopped losses with one conditional statement.
    db.statements.clear()
    await dao.clear_stopped(db, agent_id)
    assert len(db.statements) == 1
    assert db.statements[0].startswith("DELETE FROM agent_runtime_losses WHERE ")
    # Then missing/image observations win the race, and another agent's loss is untouched.
    remaining = await dao.get(db, agent_id)
    if concurrent_reason in ("container_missing", "image_unavailable"):
        assert remaining is not None
        assert remaining.reason == concurrent_reason
    else:
        assert remaining is None
    assert await dao.get(db, other_id) is not None
