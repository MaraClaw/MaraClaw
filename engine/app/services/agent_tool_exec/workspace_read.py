from __future__ import annotations

import fnmatch
import re
import uuid
from pathlib import Path


def _display_size(size_bytes: int) -> str:
    return f"{size_bytes}B" if size_bytes < 1024 else f"{size_bytes / 1024:.1f}KB"


async def _storage_list_dir(
    agent_id: uuid.UUID,
    rel_path: str,
    tenant_id: str | None = None,
    *,
    get_storage_backend,
    tool_storage_key,
    display_size,
) -> str:
    storage = get_storage_backend()
    storage_key, normalized, _is_enterprise = tool_storage_key(agent_id, rel_path, tenant_id)

    exists = await storage.exists(storage_key)
    is_dir = await storage.is_dir(storage_key)
    if exists and not is_dir:
        return f"Path is not a directory: {rel_path}"
    if not exists and not is_dir and normalized:
        return f"Directory not found: {rel_path or '/'}"

    items: list[str] = []
    dir_count = 0
    file_count = 0
    if not normalized and tenant_id:
        items.append("  📁 enterprise_info/ (shared company info)")
        dir_count += 1

    entries = await storage.list_dir(storage_key) if exists or is_dir else []
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
    get_storage_backend,
    tool_storage_key,
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
        if start >= total_lines and total_lines > 0:
            return f"Offset {offset} exceeds file length ({total_lines} lines total)"
        selected_lines = lines[start:end]
        output = "\n".join(f"{index + 1:6}\t{line}" for index, line in enumerate(selected_lines, start=start))
        if total_lines > end:
            output += f"\n\n... [{total_lines - end} more lines not shown, lines {end + 1}-{total_lines}]"
        header = f"📄 {rel_path} (lines {start + 1 if total_lines else 0}-{end} of {total_lines})\n"
        return header + output
    except Exception as error:
        return f"Read failed: {error}"


async def _storage_walk_files(storage, root_key: str) -> list[object]:
    out = []
    for entry in await storage.list_dir(root_key):
        if entry.name.startswith("."):
            continue
        out.append(entry)
        if entry.is_dir:
            out.extend(await _storage_walk_files(storage, entry.key))
    return out


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
    get_storage_backend,
    tool_storage_key,
    storage_walk_files,
    relative_storage_display,
) -> str:
    storage = get_storage_backend()
    rel_path = "" if path in ("", ".") else path
    base_key, normalized, _ = tool_storage_key(agent_id, rel_path, tenant_id)
    if not await storage.is_dir(base_key) and normalized:
        return f"Directory not found: {path}"
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as error:
        return f"Invalid regex pattern: {error}"

    results: list[str] = []
    total_matches = 0
    files_searched = 0
    entries = await storage_walk_files(storage, base_key) if await storage.is_dir(base_key) else []
    for entry in entries:
        if entry.is_dir:
            continue
        rel_display = relative_storage_display(entry.key, base_key, normalized)
        if not fnmatch.fnmatch(Path(rel_display).name, file_pattern) and not fnmatch.fnmatch(rel_display, file_pattern):
            continue
        if Path(rel_display).suffix.lower() in {
            ".pyc",
            ".pyo",
            ".so",
            ".dll",
            ".exe",
            ".bin",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".zip",
            ".tar",
            ".gz",
        }:
            continue
        files_searched += 1
        try:
            content = await storage.read_text(entry.key, encoding="utf-8", errors="ignore")
        except Exception:
            content = ""
        for index, line in enumerate(content.splitlines(), 1):
            if regex.search(line):
                results.append(f"{rel_display}:{index}: {line.strip()[:100]}")
                total_matches += 1
                if len(results) >= 50:
                    break
        if len(results) >= 50:
            break
    if not results:
        return f"No matches found for pattern '{pattern}' in {files_searched} file(s)"
    truncated = total_matches > len(results)
    truncation_note = (
        f" (showing first {len(results)} of {total_matches}+ - refine pattern or path for more)" if truncated else ""
    )
    return (
        f"🔍 Found {total_matches}+ match(es) in {files_searched} file(s) for pattern '{pattern}'{truncation_note}:\n"
        + "\n".join(results)
    )


async def _storage_find_files(
    agent_id: uuid.UUID,
    pattern: str,
    path: str = ".",
    tenant_id: str | None = None,
    *,
    get_storage_backend,
    tool_storage_key,
    storage_walk_files,
    relative_storage_display,
    display_size,
) -> str:
    storage = get_storage_backend()
    rel_path = "" if path in ("", ".") else path
    base_key, normalized, _ = tool_storage_key(agent_id, rel_path, tenant_id)
    if not await storage.is_dir(base_key) and normalized:
        return f"Directory not found: {path}"
    entries = await storage_walk_files(storage, base_key) if await storage.is_dir(base_key) else []
    matches = []
    for entry in entries:
        rel_display = relative_storage_display(entry.key, base_key, normalized)
        if fnmatch.fnmatch(rel_display, pattern) or fnmatch.fnmatch(Path(rel_display).name, pattern):
            matches.append((entry, rel_display))
    if not matches:
        return f"No files matching pattern: {pattern}"
    results = []
    dir_count = 0
    file_count = 0
    for entry, rel_display in matches[:100]:
        if entry.is_dir:
            dir_count += 1
            results.append(f"📁 {rel_display}/")
        else:
            file_count += 1
            results.append(f"📄 {rel_display} ({display_size(entry.size)})")
    return (
        f"📂 Found {len(matches)} item(s) ({dir_count} dirs, {file_count} files) matching '{pattern}':\n"
        + "\n".join(results)
    )
