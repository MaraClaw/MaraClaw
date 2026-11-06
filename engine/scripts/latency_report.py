"""Summarize ``[TurnTiming]`` and ``[RelayTiming]`` log lines as p50/p95/p99 per offset and interval.

Usage (from ``engine/``)::

    python scripts/latency_report.py engine.log [more.log ...]
    docker logs maraclaw-engine 2>&1 | python scripts/latency_report.py

Offsets are milliseconds: turn marks from ``gateway_messages.created_at``, relay marks from the source message
``completed_at``. ``adapter_*`` offsets use the dispatcher's separate process-start clock; never subtract them
from engine marks. A derived interval is skipped when either endpoint is missing.
"""

from __future__ import annotations

import math
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TextIO

from pydantic import JsonValue, TypeAdapter, ValidationError

_LINE = re.compile(r"\[(TurnTiming|RelayTiming)\] (\{.*\})")
_JSON_VALUE = TypeAdapter(JsonValue)

TURN_MARKS = (
    "claimed",
    "exec_started",
    "first_delta",
    "rpc_returned",
    "settled",
    "published",
    "adapter_hello_ok",
    "adapter_accepted",
    "adapter_terminal",
)
TURN_INTERVALS = (
    ("claimed", "exec_started"),
    ("exec_started", "first_delta"),
    ("exec_started", "rpc_returned"),
    ("first_delta", "rpc_returned"),
    ("adapter_hello_ok", "adapter_accepted"),
    ("adapter_accepted", "adapter_terminal"),
    ("rpc_returned", "settled"),
    ("settled", "published"),
)
RELAY_MARKS = ("claimed", "sent", "done")
RELAY_INTERVALS = (("claimed", "sent"),)

type Offsets = dict[str, int]


def parse_lines(lines: Iterable[str]) -> tuple[list[Offsets], list[Offsets]]:
    """Return ``(turns, relays)``: each record keeps only its integer millisecond mark offsets."""
    turns: list[Offsets] = []
    relays: list[Offsets] = []
    for line in lines:
        found = _LINE.search(line)
        if found is None:
            continue
        try:
            payload = _JSON_VALUE.validate_json(found.group(2))
        except ValidationError:
            continue
        if not isinstance(payload, dict):
            continue
        is_turn = found.group(1) == "TurnTiming"
        marks = TURN_MARKS if is_turn else RELAY_MARKS
        offsets: Offsets = {}
        for name in marks:
            value = payload.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                offsets[name] = value
        (turns if is_turn else relays).append(offsets)
    return turns, relays


def percentile(ordered: Sequence[int], q: float) -> int:
    """Nearest-rank percentile of an ascending, non-empty sequence."""
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def _rows(
    records: Sequence[Offsets], marks: Sequence[str], intervals: Sequence[tuple[str, str]]
) -> list[tuple[str, list[int]]]:
    rows = [(name, [r[name] for r in records if name in r]) for name in marks]
    rows += [(f"{a}->{b}", [r[b] - r[a] for r in records if a in r and b in r]) for a, b in intervals]
    return [(name, sorted(values)) for name, values in rows if values]


def render(title: str, records: Sequence[Offsets], marks: Sequence[str], intervals: Sequence[tuple[str, str]]) -> str:
    out = [f"{title}: {len(records)} lines", f"{'metric':<24}{'count':>7}{'p50':>9}{'p95':>9}{'p99':>9}  (ms)"]
    for name, values in _rows(records, marks, intervals):
        stats = "".join(f"{percentile(values, q):>9}" for q in (50, 95, 99))
        out.append(f"{name:<24}{len(values):>7}{stats}")
    return "\n".join(out)


def report(lines: Iterable[str]) -> str:
    turns, relays = parse_lines(lines)
    sections: list[str] = []
    if turns:
        sections.append(render("TurnTiming", turns, TURN_MARKS, TURN_INTERVALS))
    if relays:
        sections.append(render("RelayTiming", relays, RELAY_MARKS, RELAY_INTERVALS))
    return "\n\n".join(sections) if sections else "No [TurnTiming] or [RelayTiming] lines found."


def _read(paths: Sequence[str], stdin: TextIO) -> list[str]:
    if not paths:
        return stdin.read().splitlines()
    return [line for path in paths for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines()]


def main(argv: Sequence[str] | None = None) -> int:
    paths = list(sys.argv[1:] if argv is None else argv)
    print(report(_read(paths, sys.stdin)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
