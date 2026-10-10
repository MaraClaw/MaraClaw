"""Initialize a fresh database from the authoritative, rerunnable baseline.

Existing databases are not upgraded. Schema errors are fatal.
"""

from __future__ import annotations

import re
from pathlib import Path

import anyio

from app.db.pool import close_pool, init_pool
from app.db.session import connection_ctx

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")
_TX_WRAPPERS = frozenset({"BEGIN", "COMMIT", "BEGIN TRANSACTION", "COMMIT TRANSACTION"})

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_BASELINE = ROOT / "scripts" / "schema_baseline.sql"


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


async def _apply_sql_script(path: Path) -> None:
    sql = path.read_text(encoding="utf-8")  # noqa: ASYNC240 - one-shot bootstrap CLI
    async with connection_ctx() as conn:
        for body in _statement_bodies(sql):
            await conn.execute(body)


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
