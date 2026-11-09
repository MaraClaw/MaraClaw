from __future__ import annotations

import fnmatch
import re
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from pathlib import Path

from app.services.storage_runtime.base import StorageBackend, StorageEntry

from .workspace_search import search_page
from .workspace_search_cursor import InvalidSearchCursorError, SearchQuery, parse_position
from .workspace_search_scope import search_key_allowed, search_path_allowed

type StorageBackendFactory = Callable[[], StorageBackend]
type ToolStorageKeyFn = Callable[[uuid.UUID, str, str | None], tuple[str, str, bool]]
type DisplaySizeFn = Callable[[int], str]
type StorageWalkFn = Callable[[StorageBackend, str], AsyncGenerator[StorageEntry]]
type RelativeDisplayFn = Callable[[str, str, str], str]


def _display_size(size_bytes: int) -> str:
    return f"{size_bytes}B" if size_bytes < 1024 else f"{size_bytes / 1024:.1f}KB"


async def _storage_list_dir(
    agent_id: uuid.UUID,
    rel_path: str,
    tenant_id: str | None = None,
    *,
    get_storage_backend: StorageBackendFactory,
    tool_storage_key: ToolStorageKeyFn,
    display_size: DisplaySizeFn,
) -> str:
    storage = get_storage_backend()
    storage_key, normalized, _is_enterprise = tool_storage_key(agent_id, rel_path, tenant_id)

    is_dir = await storage.is_dir(storage_key)
    if not is_dir and await storage.exists(storage_key):
        return f"Path is not a directory: {rel_path}"
    if not is_dir and normalized:
        return f"Directory not found: {rel_path or '/'}"

    items: list[str] = []
    dir_count = 0
    file_count = 0
    if not normalized and tenant_id not in (None, ""):
        items.append("  📁 enterprise_info/ (shared company info)")
        dir_count += 1

    entries = await storage.list_dir(storage_key) if is_dir else []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_dir:
            dir_count += 1
            try:
                child_count = len(
                    [child for child in await storage.list_dir(entry.key) if not child.name.startswith(".")]
                )
            except Exception:
                child_count = 0
            items.append(f"  📁 {entry.name}/ ({child_count} items)")
        else:
            file_count += 1
            items.append(f"  📄 {entry.name} ({display_size(entry.size)})")

    if not items:
        return f"📂 {rel_path or 'root'}: Empty directory (0 files, 0 folders)"
    header = f"📂 {rel_path or 'root'}: {dir_count} folder(s), {file_count} file(s)\n"
    return header + "\n".join(items)


async def _storage_read_file(
    agent_id: uuid.UUID,
    rel_path: str,
    tenant_id: str | None = None,
    offset: int = 0,
    limit: int = 2000,
    *,
    get_storage_backend: StorageBackendFactory,
    tool_storage_key: ToolStorageKeyFn,
) -> str:
    storage = get_storage_backend()
    storage_key, normalized, _ = tool_storage_key(agent_id, rel_path, tenant_id)
    if not normalized:
        return "File not found: root"
    if not await storage.is_file(storage_key):
        return f"File not found: {rel_path}"
    try:
        content = await storage.read_text(storage_key, encoding="utf-8", errors="replace")
        lines = content.splitlines()
        total_lines = len(lines)
        start = max(0, offset)
        end = min(total_lines, start + limit)
        if start >= total_lines > 0:
            return f"Offset {offset} exceeds file length ({total_lines} lines total)"
        selected_lines = lines[start:end]
        output = "\n".join(f"{index + 1:6}\t{line}" for index, line in enumerate(selected_lines, start=start))
        if total_lines > end:
            output += f"\n\n... [{total_lines - end} more lines not shown, lines {end + 1}-{total_lines}]"
        header = f"📄 {rel_path} (lines {start + 1 if total_lines else 0}-{end} of {total_lines})\n"
        return header + output
    except Exception as error:
        return f"Read failed: {error}"


def _storage_walk_files(storage: StorageBackend, root_key: str) -> AsyncGenerator[StorageEntry]:
    return storage.walk(root_key)


def _relative_storage_display(entry_key: str, base_key: str, display_base: str) -> str:
    rel = entry_key.removeprefix(base_key.rstrip("/") + "/")
    return f"{display_base.rstrip('/')}/{rel}".strip("/") if display_base else rel


async def _storage_search_files(
    agent_id: uuid.UUID,
    pattern: str,
    path: str = ".",
    file_pattern: str = "*",
    ignore_case: bool = False,
    tenant_id: str | None = None,
    *,
    cursor: str | None = None,
    limit: int = 50,
    get_storage_backend: StorageBackendFactory,
    tool_storage_key: ToolStorageKeyFn,
    storage_walk_files: StorageWalkFn,
    relative_storage_display: RelativeDisplayFn,
) -> str:
    del storage_walk_files, relative_storage_display
    if isinstance(limit, bool) or not 1 <= limit <= 50:
        return "Invalid search_files limit: expected an integer between 1 and 50"
    if not search_path_allowed(path):
        return "Access denied for this path"
    storage = get_storage_backend()
    rel_path = "" if path in ("", ".") else path
    base_key, normalized, enterprise = tool_storage_key(agent_id, rel_path, tenant_id)
    if enterprise and tenant_id in (None, ""):
        return f"Directory not found: {path}"
    query = SearchQuery(
        agent=str(agent_id),
        tenant=tenant_id,
        root=base_key,
        display=normalized,
        pattern=pattern,
        file_pattern=file_pattern,
        ignore_case=ignore_case,
        limit=limit,
    )
    if not search_key_allowed(storage, base_key, base_key):
        return "Access denied for this path"
    try:
        position = await parse_position(cursor, query)
        if cursor is None and normalized and not await storage.is_dir(base_key):
            return f"Directory not found: {path}"
        return await search_page(storage, position)
    except re.error as error:
        return f"Invalid regex pattern: {error}"
    except InvalidSearchCursorError as error:
        return str(error)


async def _storage_find_files(
    agent_id: uuid.UUID,
    pattern: str,
    path: str = ".",
    tenant_id: str | None = None,
    *,
    get_storage_backend: StorageBackendFactory,
    tool_storage_key: ToolStorageKeyFn,
    storage_walk_files: StorageWalkFn,
    relative_storage_display: RelativeDisplayFn,
    display_size: DisplaySizeFn,
) -> str:
    storage = get_storage_backend()
    rel_path = "" if path in ("", ".") else path
    base_key, normalized, _ = tool_storage_key(agent_id, rel_path, tenant_id)
    is_dir = await storage.is_dir(base_key)
    if not is_dir and normalized:
        return f"Directory not found: {path}"
    total = 0
    shown: list[tuple[StorageEntry, str]] = []
    if is_dir:
        async with aclosing(storage_walk_files(storage, base_key)) as entries:
            async for entry in entries:
                rel_display = relative_storage_display(entry.key, base_key, normalized)
                if fnmatch.fnmatch(rel_display, pattern) or fnmatch.fnmatch(Path(rel_display).name, pattern):
                    total += 1
                    if len(shown) < 100:
                        shown.append((entry, rel_display))
    if not total:
        return f"No files matching pattern: {pattern}"
    results: list[str] = []
    dir_count = 0
    file_count = 0
    for entry, rel_display in shown:
        if entry.is_dir:
            dir_count += 1
            results.append(f"📁 {rel_display}/")
        else:
            file_count += 1
            results.append(f"📄 {rel_display} ({display_size(entry.size)})")
    return f"📂 Found {total} item(s) ({dir_count} dirs, {file_count} files) matching '{pattern}':\n" + "\n".join(
        results
    )
