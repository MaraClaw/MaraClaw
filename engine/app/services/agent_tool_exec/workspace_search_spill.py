"""Immutable continuation state shared by workers using the same agent data directory.

Incomplete logical lines and oversized traversal positions spill. Retaining
predecessors makes cursor retries and branches safe. Expired rows are reclaimed in
bounded batches on later writes; SQLite reuses freed pages. Final Python regex
evaluation needs one complete line in memory, regardless of its length; neither
that memory nor regex time is bounded.
"""

import sqlite3
from contextlib import closing
from pathlib import Path
from time import time
from typing import ClassVar
from uuid import uuid4

import anyio
from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.config import get_settings

from .workspace_search_cursor import CURSOR_TTL, InvalidSearchCursorError, _cipher


class _Fragment(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", strict=True)

    previous: str = Field(pattern=r"^(?:[0-9a-f]{32})?$")
    text: str


def _cache_path() -> Path:
    return Path(get_settings().AGENT_DATA_DIR).resolve() / ".workspace-search" / "lines.sqlite3"


def _append(previous: str, text: str, started: int) -> str:
    path = _cache_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = uuid4().hex
    payload = _cipher().encrypt(_Fragment(previous=previous, text=text).model_dump_json().encode())
    with closing(sqlite3.connect(path, timeout=5)) as connection, connection:
        _ = connection.execute(
            "CREATE TABLE IF NOT EXISTS fragments (token TEXT PRIMARY KEY, payload BLOB NOT NULL, expires INTEGER NOT NULL)"
        )
        _ = connection.execute("CREATE INDEX IF NOT EXISTS fragments_expiry ON fragments(expires)")
        _ = connection.execute(
            "DELETE FROM fragments WHERE token IN (SELECT token FROM fragments WHERE expires < ? LIMIT 128)",
            (int(time()),),
        )
        _ = connection.execute("INSERT INTO fragments VALUES (?, ?, ?)", (token, payload, started + CURSOR_TTL + 60))
    return token


def _read(tail: str) -> str:
    parts: list[str] = []
    try:
        with closing(sqlite3.connect(f"{_cache_path().as_uri()}?mode=ro", uri=True, timeout=5)) as connection:
            while tail:
                row = TypeAdapter(tuple[bytes] | None).validate_python(
                    connection.execute("SELECT payload FROM fragments WHERE token = ?", (tail,)).fetchone(), strict=True
                )
                if row is None:
                    raise InvalidSearchCursorError
                fragment = _Fragment.model_validate_json(_cipher().decrypt(row[0]))
                parts.append(fragment.text)
                tail = fragment.previous
    except sqlite3.Error, InvalidToken, ValueError:
        raise InvalidSearchCursorError from None
    return "".join(reversed(parts))


async def append_fragment(previous: str, text: str, started: int) -> str:
    try:
        return await anyio.to_thread.run_sync(_append, previous, text, started)
    except OSError, sqlite3.Error:
        raise InvalidSearchCursorError from None


async def read_fragments(tail: str) -> str:
    return await anyio.to_thread.run_sync(_read, tail, abandon_on_cancel=True)
