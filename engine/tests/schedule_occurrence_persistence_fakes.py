from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import final
from uuid import UUID

from app.records.schedule import AgentScheduleRecord

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
NEXT = NOW + timedelta(days=1)
SLOT_COLUMNS = (
    "pending_due_at",
    "pending_instruction",
    "lease_token",
    "lease_expires_at",
    "pending_started_at",
    "pending_notify_on_failure",
)


@final
class _Cursor:
    def __init__(self, raw: _Raw) -> None:
        self.raw = raw
        self.rows: list[dict[str, object]] = []

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, query: str, params: Mapping[str, object]) -> None:
        sql = " ".join(query.split())
        self.raw.executed.append((sql, params))
        self.rows = []
        pool = self.raw.pool
        if sql.startswith("SELECT"):
            if "FOR UPDATE SKIP LOCKED" in sql:
                assert "WHERE is_enabled IS TRUE AND (" in sql
                assert "(pending_due_at IS NULL AND next_run_at <= %(now)s " in sql
                assert "AND (ends_at IS NULL OR next_run_at < ends_at)) OR " in sql
                assert "(pending_due_at IS NOT NULL AND lease_expires_at <= %(now)s)" in sql
                now = params["now"]
                assert isinstance(now, datetime)
                for row in pool.rows.values():
                    due = (
                        row.pending_due_at is None
                        and row.next_run_at is not None
                        and row.next_run_at <= now
                        and (row.ends_at is None or row.next_run_at < row.ends_at)
                    )
                    expired = (
                        row.pending_due_at is not None
                        and row.lease_expires_at is not None
                        and row.lease_expires_at <= now
                    )
                    if row.is_enabled and (due or expired) and row.id not in pool.locked:
                        pool.locked.add(row.id)
                        self.raw.locked.add(row.id)
                        self.rows.append(asdict(row))
                if pool.pause_first and len(pool.connections) == 1:
                    pool.selected.set()
                    await pool.release.wait()
            else:
                assert "WHERE id = %(id)s" in sql
                schedule_id = params["id"]
                assert isinstance(schedule_id, UUID)
                row = pool.rows.get(schedule_id)
                self.rows = [asdict(row)] if row is not None else []
            return
        if "agent_id IN (" in sql:
            assert "WHERE is_enabled IS TRUE" in sql
            assert all(f"{column} = NULL" in sql for column in SLOT_COLUMNS)
            for row in list(pool.rows.values()):
                tenant_id, creator_id = pool.agents[row.agent_id]
                if (
                    row.is_enabled
                    and ("tenant_id" not in params or params["tenant_id"] == tenant_id)
                    and ("creator_id" not in params or params["creator_id"] == creator_id)
                ):
                    self._write(row, {"is_enabled": False, "next_run_at": None, **dict.fromkeys(SLOT_COLUMNS)})
            return
        schedule_id = params["id"]
        assert isinstance(schedule_id, UUID)
        row = pool.rows.get(schedule_id)
        if row is None:
            return
        changes: dict[str, object]
        if "token" in params:
            assert "is_enabled IS TRUE AND pending_due_at IS NOT NULL" in sql
            assert "lease_token = %(token)s AND lease_expires_at > %(now)s" in sql
            now = params["now"]
            assert isinstance(now, datetime)
            if not (
                row.is_enabled
                and row.pending_due_at is not None
                and row.lease_token == params["token"]
                and row.lease_expires_at is not None
                and row.lease_expires_at > now
            ):
                return
            if "CASE WHEN pending_started_at IS NULL" in sql:
                assert "pending_started_at = COALESCE(pending_started_at, %(now)s)" in sql
                assert "ELSE run_count END" in sql
                assert "ELSE last_run_at END" in sql
                changes = (
                    {"pending_started_at": now, "last_run_at": now, "run_count": row.run_count + 1}
                    if row.pending_started_at is None
                    else {}
                )
            elif "lease_expires_at = %(lease_expires_at)s" in sql:
                changes = {"lease_expires_at": params["lease_expires_at"]}
            else:
                assert all(f"{column} = NULL" in sql for column in SLOT_COLUMNS)
                changes = {}
                changes.update(dict.fromkeys(SLOT_COLUMNS))
        elif "run_count = COALESCE" in sql:
            assert sql == (
                "UPDATE agent_schedules SET run_count = COALESCE(run_count, 0) + 1, last_run_at = %(now)s "
                "WHERE id = %(id)s"
            )
            changes = {
                "run_count": (pool.counts.get(schedule_id, row.run_count) or 0) + 1,
                "last_run_at": params["now"],
            }
        elif "lease_token = %(lease_token)s" in sql and "is_enabled" not in params:
            assert schedule_id in self.raw.locked
            changes = {key: value for key, value in params.items() if key != "id"}
            if "pending_due_at = next_run_at" in sql:
                assert "pending_instruction = instruction" in sql
                assert "pending_notify_on_failure = notify_on_failure" in sql
                assert "pending_started_at = NULL" in sql
                changes.update(
                    pending_due_at=row.next_run_at,
                    pending_instruction=row.instruction,
                    pending_notify_on_failure=row.notify_on_failure,
                    pending_started_at=None,
                )
            else:
                assert "next_run_at" not in params
        elif "is_enabled = FALSE" in sql:
            assert schedule_id in self.raw.locked
            assert all(f"{column} = NULL" in sql for column in SLOT_COLUMNS)
            changes = {"is_enabled": False, "next_run_at": None, **dict.fromkeys(SLOT_COLUMNS)}
        else:
            assert sql.startswith("UPDATE agent_schedules SET ")
            assert "RETURNING" in sql
            changes = {key: value for key, value in params.items() if key != "id"}
        self._write(row, changes)

    def _write(self, row: AgentScheduleRecord, changes: Mapping[str, object]) -> None:
        self.raw.before.setdefault(row.id, (row, self.raw.pool.counts.get(row.id)))
        updated = AgentScheduleRecord.from_row(asdict(row) | dict(changes))
        self.raw.pool.rows[row.id] = updated
        self.raw.pool.counts[row.id] = updated.run_count
        self.rows.append(asdict(updated))

    async def fetchall(self) -> list[dict[str, object]]:
        return self.rows

    async def fetchone(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None


@final
class _Raw:
    def __init__(self, pool: _Pool) -> None:
        self.pool = pool
        self.locked: set[UUID] = set()
        self.executed: list[tuple[str, Mapping[str, object]]] = []
        self.commits = 0
        self.before: dict[UUID, tuple[AgentScheduleRecord, int | None]] = {}

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    async def commit(self) -> None:
        self.commits += 1
        self.before.clear()
        self.pool.locked.difference_update(self.locked)

    async def rollback(self) -> None:
        for schedule_id, (row, count) in self.before.items():
            self.pool.rows[schedule_id] = row
            self.pool.counts[schedule_id] = count
        self.before.clear()
        self.pool.locked.difference_update(self.locked)


@final
class _Pool:
    def __init__(self) -> None:
        self.rows: dict[UUID, AgentScheduleRecord] = {}
        self.counts: dict[UUID, int | None] = {}
        self.agents: dict[UUID, tuple[UUID, UUID]] = {}
        self.locked: set[UUID] = set()
        self.connections: list[_Raw] = []
        self.next_run: datetime = NEXT
        self.pause_first = False
        self.selected = asyncio.Event()
        self.release = asyncio.Event()

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[_Raw]:
        raw = _Raw(self)
        self.connections.append(raw)
        yield raw
