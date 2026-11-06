"""A separate-process SQLite writer standing in for a running guest that holds its WAL database open."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from multiprocessing import get_context
from multiprocessing.connection import Connection
from pathlib import Path

ANSWER_SECONDS = 30


def _status(database: sqlite3.Connection) -> str:
    integrity = database.execute("PRAGMA integrity_check").fetchone()[0]
    rows = database.execute("SELECT count(*) FROM events").fetchone()[0]
    return f"{integrity} {rows}"


def _guest(path: str, channel: Connection) -> None:
    # Autocommit with the WAL never checkpointed, so committed rows live only in the -wal file.
    database = sqlite3.connect(path, isolation_level=None)
    _ = database.execute("PRAGMA journal_mode=WAL")
    _ = database.execute("PRAGMA wal_autocheckpoint=0")
    _ = database.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
    channel.send("ready")
    while (command := channel.recv()) != "stop":
        try:
            if command == "insert":
                _ = database.execute("INSERT INTO events (body) VALUES (?)", ("x" * 512,))
            channel.send(_status(database))
        except sqlite3.Error as error:
            channel.send(f"error: {error}")


@dataclass(frozen=True, slots=True)
class SqliteGuest:
    channel: Connection

    def command(self, name: str) -> str:
        """``insert`` commits one row; both commands answer ``<integrity_check> <row count>``."""
        self.channel.send(name)
        if not self.channel.poll(ANSWER_SECONDS):
            raise TimeoutError(f"SQLite guest did not answer {name!r}")
        return str(self.channel.recv())


@contextmanager
def running_sqlite_guest(path: Path) -> Generator[SqliteGuest]:
    context = get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_guest, args=(str(path), child))
    process.start()
    child.close()
    try:
        if not parent.poll(ANSWER_SECONDS) or parent.recv() != "ready":
            raise TimeoutError("SQLite guest did not open its database")
        yield SqliteGuest(parent)
    finally:
        with suppress(OSError):
            parent.send("stop")
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
        parent.close()
