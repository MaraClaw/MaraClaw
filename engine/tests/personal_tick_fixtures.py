"""Observe a personal bridge child's one-second tick on real PostgreSQL: the SQL it issues, the frames it writes."""

import re
from collections.abc import Awaitable, Callable

import anyio
import pytest
from psycopg import AsyncCursor
from pydantic import JsonValue

from app.db.connection import DbConnection, Params
from app.db.session import get_connection, transaction

type Statement = tuple[str, Params]


class TickStoppedError(Exception):
    """Raised by the replaced one-second sleep once every scripted tick has run."""


def record_statements(monkeypatch: pytest.MonkeyPatch) -> list[Statement]:
    """Capture each statement that reaches PostgreSQL with its parameters."""
    statements: list[Statement] = []
    execute = AsyncCursor.execute

    async def recorded(self, query, *args, **kwargs):
        statements.append((str(query), args[0] if args else None))
        return await execute(self, query, *args, **kwargs)

    monkeypatch.setattr(AsyncCursor, "execute", recorded)
    return statements


def record_frames(monkeypatch: pytest.MonkeyPatch, child_type: type) -> list[dict[str, JsonValue]]:
    """Replace the bridge write; no database connection may be held while a frame is written."""
    frames: list[dict[str, JsonValue]] = []

    async def write(_self, value):
        assert get_connection() is None
        frames.append(value)

    monkeypatch.setattr(child_type, "write", write)
    return frames


def between_ticks(monkeypatch: pytest.MonkeyPatch, *actions: Callable[[], Awaitable[None]]) -> None:
    """Run each action in place of one tick's sleep, then end the tick on the next sleep."""
    remaining = list(actions)

    async def sleep(_seconds):
        if not remaining:
            raise TickStoppedError
        await remaining.pop(0)()

    monkeypatch.setattr(anyio, "sleep", sleep)


def queries(statements: list[Statement], containing: str = "") -> list[str]:
    return [query for query, _ in statements if containing in query]


def touching(statements: list[Statement], prefix: str) -> list[tuple[str, str]]:
    """The verb and table of each statement on a table named with the prefix, in the order issued."""
    found = [(query.split()[0], re.search(r"\b(?:FROM|UPDATE|INTO)\s+(\w+)", query)) for query, _ in statements]
    return [(verb, table.group(1)) for verb, table in found if table is not None and table.group(1).startswith(prefix)]


async def planned_with_index(db: DbConnection, statement: Statement) -> str:
    """The plan a recorded statement gets when only an index can beat a sequential scan."""
    query, params = statement
    async with transaction(db):
        await db.execute("SET LOCAL enable_seqscan=off")
        rows = await db.fetchall(f"EXPLAIN (COSTS OFF) {query}", params)
    return "\n".join(str(value) for row in rows for value in row.values())
