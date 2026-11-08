"""Resumable search using bounded directory pages and origin content windows.

Budgets bound origin reads, returned entries and completed lines, not backend
listing internals, spill reconstruction, or Python regex time/memory per line.
"""

import fnmatch
import json
import re
from pathlib import PurePosixPath
from typing import Final, Literal, assert_never

import anyio
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException

from app.services.storage_runtime.base import (
    StorageBackend,
    StorageConditionalRaceError,
    walk_order,
)
from app.services.storage_runtime.fallback import FallbackStorageBackend

from .workspace_search_content import SearchLine, feed_window, take_line
from .workspace_search_cursor import (
    CONTENT_BYTES,
    FILE_BYTES,
    LINE_STEPS,
    SCAN_STEPS,
    DirectoryPosition,
    FilePosition,
    SearchPosition,
    SearchQuery,
)
from .workspace_search_scope import search_key_allowed

_BINARY_SUFFIXES: Final = frozenset(
    {".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".png", ".jpg", ".jpeg", ".gif", ".zip", ".tar", ".gz"}
)
type StopReason = Literal["exhausted", "result_limit", "scan_budget", "content_budget", "line_budget"]


async def search_page(storage: StorageBackend, position: SearchPosition) -> str:
    query = position.query
    regex = re.compile(query.pattern, re.IGNORECASE if query.ignore_case else 0)
    directories = list(position.directories)
    pending = position.pending
    unreadable = position.unreadable
    results: list[str] = []
    issues: list[str] = []
    files_searched: set[str] = set()
    steps = content_bytes = lines_scanned = 0
    reason: StopReason = "exhausted"
    read_amplification = _read_amplification(storage)
    reservation = FILE_BYTES * read_amplification
    if reservation > CONTENT_BYTES:
        return "Search failed: configured storage read amplification exceeds content budget"
    while directories or pending is not None:
        await anyio.lowlevel.checkpoint()
        while directories and directories[-1].done:
            directories.pop()
        if not directories and pending is None:
            break
        if len(results) == query.limit:
            reason = "result_limit"
            break
        if lines_scanned == LINE_STEPS:
            reason = "line_budget"
            break
        if pending is not None:
            files_searched.add(pending.key)
            match await take_line(pending, position.started):
                case SearchLine(text=text, number=line_number, rest=rest):
                    if regex.search(text):
                        results.append(f"{_display(pending.key, query)}:{line_number}: {text.strip()[:100]}")
                    lines_scanned += 1
                    pending = rest
                    continue
                case None:
                    pending = None
                    continue
                case FilePosition() as window:
                    pending = window
                case unreachable:
                    assert_never(unreachable)
            if CONTENT_BYTES - content_bytes < reservation:
                reason = "content_budget"
                break
            display = _display(pending.key, query)
            if not search_key_allowed(storage, pending.key, query.root):
                issues.append(f"{display}: access denied")
                unreadable += 1
                pending = None
                continue
            try:
                data = await storage.read_bytes_bounded(pending.key, FILE_BYTES, offset=pending.offset)
            except OSError, HTTPException, BotoCoreError, ClientError, StorageConditionalRaceError:
                content_bytes += reservation
                issues.append(f"{display}: content unreadable")
                unreadable += 1
                pending = None
            else:
                content_bytes += len(data) * read_amplification
                pending = feed_window(pending, data, FILE_BYTES)
            continue
        if steps == SCAN_STEPS:
            reason = "scan_budget"
            break
        directory = directories[-1]
        if not search_key_allowed(storage, directory.key, query.root):
            issues.append(f"{_display(directory.key, query)}: access denied")
            unreadable += 1
            directories.pop()
            continue
        steps += 1
        try:
            page = await storage.list_dir_page(directory.key, limit=1, after=directory.after)
        except OSError, HTTPException, BotoCoreError, ClientError:
            issues.append(f"{_display(directory.key, query)}: directory unreadable")
            unreadable += 1
            directories.pop()
            continue
        if not page.entries:
            if page.has_more:
                return "Search failed: storage listing returned no resumable position"
            directories.pop()
            continue
        entry = page.entries[0]
        order = walk_order(entry)
        if order <= directory.after or entry.key != f"{directory.key}/{entry.name}":
            return "Search failed: invalid storage listing position"
        directories[-1] = DirectoryPosition(key=directory.key, after=order, done=not page.has_more)
        if entry.name.startswith(".") or not search_key_allowed(storage, entry.key, query.root):
            continue
        if entry.is_dir:
            if directories[-1].done:
                directories.pop()
            directories.append(DirectoryPosition(key=entry.key))
            continue
        display = _display(entry.key, query)
        if not (
            fnmatch.fnmatch(PurePosixPath(display).name, query.file_pattern)
            or fnmatch.fnmatch(display, query.file_pattern)
        ):
            continue
        if PurePosixPath(display).suffix.lower() in _BINARY_SUFFIXES:
            continue
        pending = FilePosition(key=entry.key)
    exhausted = not directories and pending is None
    next_position = SearchPosition(
        query=query, directories=tuple(directories), pending=pending, unreadable=unreadable, started=position.started
    )
    metadata = {
        "exhausted": exhausted,
        "complete": exhausted and unreadable == 0,
        "stop_reason": reason,
        "next_cursor": None if exhausted else await next_position.encode(),
        "unreadable_files": unreadable,
        "issues": issues,
        "entries_scanned": steps,
        "content_bytes": content_bytes,
        "lines_scanned": lines_scanned,
    }
    header = (
        f"🔍 Found {len(results)}+ match(es) in {len(files_searched)} file(s) for pattern '{query.pattern}':"
        if results
        else f"No matches found for pattern '{query.pattern}' in {len(files_searched)} file(s)"
    )
    return "\n".join([header, *results, "Search page: " + json.dumps(metadata, ensure_ascii=True)])


def _display(key: str, query: SearchQuery) -> str:
    relative = key.removeprefix(query.root.rstrip("/") + "/")
    return f"{query.display.rstrip('/')}/{relative}".strip("/") if query.display else relative


def _read_amplification(storage: StorageBackend) -> int:
    if isinstance(storage, FallbackStorageBackend):
        return _read_amplification(storage.primary) + _read_amplification(storage.fallback)
    return 1
