"""Prove firecrawl-anydoc imports and converts office documents.

Intended to run in the production Python 3.14 image:

    python /app/scripts/verify_anydoc.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import anydoc


def main() -> int:
    print(f"python={sys.version.split()[0]}")
    print(f"anydoc_file={anydoc.__file__}")
    fixture = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "anydoc" / "supported.docx"
    if not fixture.is_file():
        print(f"missing fixture: {fixture}", file=sys.stderr)
        return 1
    markdown = anydoc.to_markdown(fixture)
    if "Quarterly Report" not in markdown:
        print(f"unexpected markdown:\n{markdown[:400]}", file=sys.stderr)
        return 1
    print("convert_ok=supported.docx")
    print(markdown[:80])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
