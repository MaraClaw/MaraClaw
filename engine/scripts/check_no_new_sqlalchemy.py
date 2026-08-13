#!/usr/bin/env python3
"""Fail if any app module imports SQLAlchemy.

Greenfield deploys use pure psycopg + scripts/schema_baseline.sql.
Allowlist must stay empty. Maintenance rewrite:

  python scripts/check_no_new_sqlalchemy.py --write-allowlist
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "app"
ALLOWLIST_PATH = ROOT / "scripts" / "sqlalchemy_import_allowlist.txt"
# Packages that must never import SQLAlchemy.
FORBIDDEN_PREFIXES = ("app/db/",)


def _rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def files_importing_sqlalchemy() -> list[str]:
    found: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "sqlalchemy" in text:
            found.append(_rel(path))
    return sorted(found)


def load_allowlist() -> set[str]:
    if not ALLOWLIST_PATH.is_file():
        return set()
    lines = ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.strip().startswith("#")}


def write_allowlist(paths: list[str]) -> None:
    ALLOWLIST_PATH.write_text("\n".join(paths) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-allowlist",
        action="store_true",
        help="Rewrite the allowlist from the current tree (migration maintenance only).",
    )
    args = parser.parse_args(argv)

    current = files_importing_sqlalchemy()
    if args.write_allowlist:
        write_allowlist(current)
        print(f"Wrote {len(current)} paths to {ALLOWLIST_PATH.relative_to(ROOT)}")
        return 0

    if not ALLOWLIST_PATH.is_file():
        print(
            f"error: missing allowlist at {ALLOWLIST_PATH.relative_to(ROOT)}; run with --write-allowlist once",
            file=sys.stderr,
        )
        return 2

    allowlist = load_allowlist()
    # Empty allowlist is valid after Phase-4 hard cutover (no SQLAlchemy under app/).

    errors: list[str] = []
    for path in current:
        if any(path.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
            errors.append(f"forbidden SQLAlchemy import in new data layer: {path}")
            continue
        if path not in allowlist:
            errors.append(f"new SQLAlchemy import not on allowlist: {path}")

    # Allowlist paths that disappeared are fine (migration progress).
    if errors:
        print("SQLAlchemy freeze check failed:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print(
            "\nIf this is intentional migration progress, update callers to app.db. "
            "Do not grow the allowlist except when temporarily unavoidable.",
            file=sys.stderr,
        )
        return 1

    print(f"SQLAlchemy freeze OK ({len(current)} allowlisted files still import SQLAlchemy)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
