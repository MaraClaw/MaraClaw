#!/usr/bin/env python3
"""Fail if app modules import loguru directly.

Application code must use ``app.core.logging``. Bundled skill-creator
payloads are allowed to keep their upstream loguru imports.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "app"
ALLOWED_PREFIXES = ("app/services/skill_creator_files/",)


def _rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def files_importing_loguru() -> list[str]:
    found: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if (
            "import loguru" in text
            or "from loguru" in text
            or 'import_module("loguru")' in text
            or "import_module('loguru')" in text
        ):
            found.append(_rel(path))
    return sorted(found)


def main() -> int:
    offenders = [
        path for path in files_importing_loguru() if not any(path.startswith(prefix) for prefix in ALLOWED_PREFIXES)
    ]
    if not offenders:
        return 0
    print("error: app modules must import logging from app.core.logging, not loguru:", file=sys.stderr)
    for path in offenders:
        print(f"  {path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
