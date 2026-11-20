"""Initialize a fresh database from the authoritative, rerunnable baseline.

The only supported legacy upgrades are the explicit guarded ``DO`` blocks in the baseline (``tasks`` lease columns,
organization admission provisioning, ``approval_requests.execution_args``, ``gateway_messages.reply_binding_id``,
``chat_history_changes.recorded_at``, ``users.membership_generation``, gateway report plan versions),
plus the Telegram/Zalo channel enum additions.
Schema errors are fatal, except lock timeouts and deadlocks: those retry the whole transaction a few times.
"""

from __future__ import annotations

import re
from pathlib import Path

import anyio
import psycopg

from app.db.errors import DbError
from app.db.pool import close_pool, init_pool
from app.db.session import connection_ctx, long_running_statement

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")
_TX_WRAPPERS = frozenset({"BEGIN", "COMMIT", "BEGIN TRANSACTION", "COMMIT TRANSACTION"})

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_BASELINE = ROOT / "scripts" / "schema_baseline.sql"

# Concurrent runs (every replica reruns the baseline at start) queue on this transaction-level lock before any DDL.
_ADVISORY_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended(%(key)s, 0))"
_ADVISORY_LOCK_KEY = "maraclaw:schema-bootstrap"
# Bounds every later lock wait of the transaction. `set_config(..., true)` is `SET LOCAL`, which takes no bind parameter.
_LOCK_TIMEOUT_SQL = "SELECT set_config('lock_timeout', %(milliseconds)s, true)"
_LOCK_TIMEOUT_MS = 5_000
_MAX_ATTEMPTS = 5
_RETRY_DELAY_SECONDS = 1.0
_LOCK_NOT_AVAILABLE = "55P03"
_DEADLOCK_DETECTED = "40P01"


def _clean_statement(stmt: str) -> str:
    body = "\n".join(line for line in stmt.splitlines() if not line.strip().startswith("--")).strip()
    if body.upper() in _TX_WRAPPERS:
        return ""
    return body


def _statement_bodies(sql: str) -> list[str]:
    """Split SQL on top-level semicolons.

    Semicolons inside ``--`` / ``/* */`` comments, quoted strings, and
    dollar-quoted bodies (``DO $$ ... $$``) are not statement boundaries.
    Transaction wrappers are dropped because ``connection_ctx`` already commits.
    """
    bodies: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    in_line_comment = False
    in_block_comment = False
    in_single = False
    in_double = False
    dollar_tag: str | None = None

    def flush() -> None:
        body = _clean_statement("".join(buf))
        buf.clear()
        if body:
            bodies.append(body)

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                i += 2
                in_block_comment = False
                continue
            i += 1
            continue

        if in_single:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":
                    buf.append(nxt)
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        if in_double:
            buf.append(ch)
            if ch == '"':
                if nxt == '"':
                    buf.append(nxt)
                    i += 2
                    continue
                in_double = False
            i += 1
            continue

        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue

        if ch == "-" and nxt == "-":
            buf.append(ch)
            buf.append(nxt)
            i += 2
            in_line_comment = True
            continue
        if ch == "/" and nxt == "*":
            buf.append(ch)
            buf.append(nxt)
            i += 2
            in_block_comment = True
            continue
        if ch == "'":
            buf.append(ch)
            i += 1
            in_single = True
            continue
        if ch == '"':
            buf.append(ch)
            i += 1
            in_double = True
            continue
        if ch == "$":
            match = _DOLLAR_TAG.match(sql, i)
            if match:
                dollar_tag = match.group(0)
                buf.append(dollar_tag)
                i += len(dollar_tag)
                continue
        if ch == ";":
            flush()
            i += 1
            continue

        buf.append(ch)
        i += 1

    flush()
    return bodies


def _is_retryable(exc: DbError | psycopg.Error) -> bool:
    cause = exc.orig if isinstance(exc, DbError) else exc
    return isinstance(cause, psycopg.Error) and cause.sqlstate in (_LOCK_NOT_AVAILABLE, _DEADLOCK_DETECTED)


async def _apply_once(bodies: list[str]) -> None:
    # Lifting statement_timeout comes first so waiting for a peer's run is not cut off by it. The advisory lock then
    # precedes the lock bound, so the wait for the peer is unbounded and only waits on the schema itself are not.
    async with connection_ctx() as conn, long_running_statement(conn):
        await conn.execute(_ADVISORY_LOCK_SQL, {"key": _ADVISORY_LOCK_KEY})
        await conn.execute(_LOCK_TIMEOUT_SQL, {"milliseconds": str(_LOCK_TIMEOUT_MS)})
        for body in bodies:
            await conn.execute(body)


async def _apply_sql_script(path: Path) -> None:
    sql = path.read_text(encoding="utf-8")  # noqa: ASYNC240 - one-shot bootstrap CLI
    bodies = _statement_bodies(sql)
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            await _apply_once(bodies)
        except (DbError, psycopg.Error) as exc:
            if attempt == _MAX_ATTEMPTS or not _is_retryable(exc):
                raise
            print(f"[bootstrap] Attempt {attempt}/{_MAX_ATTEMPTS} rolled back ({exc}); retrying", flush=True)
            await anyio.sleep(_RETRY_DELAY_SECONDS * attempt)
        else:
            return


async def main() -> None:
    try:
        _ = await init_pool()
        print(f"[bootstrap] Applying baseline schema from {SCHEMA_BASELINE}", flush=True)
        await _apply_sql_script(SCHEMA_BASELINE)
        print("[bootstrap] Done", flush=True)
    finally:
        await close_pool()


if __name__ == "__main__":
    anyio.run(main)
