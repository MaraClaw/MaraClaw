from __future__ import annotations

from pathlib import Path


def _list_files(ws: Path, rel_path: str, tenant_id: str | None = None, *, workspace_root: Path) -> str:
    if rel_path and rel_path.startswith("enterprise_info"):
        enterprise_root = (
            (workspace_root / f"enterprise_info_{tenant_id}").resolve()
            if tenant_id
            else (workspace_root / "enterprise_info").resolve()
        )
        sub = rel_path[len("enterprise_info") :].lstrip("/")
        target = (enterprise_root / sub).resolve() if sub else enterprise_root
        if not target.is_relative_to(enterprise_root):
            return "Access denied for this path"
    else:
        target = (ws / rel_path) if rel_path else ws
        target = target.resolve()
        if not target.is_relative_to(ws.resolve()):
            return "Access denied for this path"

    if not target.exists():
        return f"Directory not found: {rel_path or '/'}"

    items = []
    if not rel_path:
        enterprise_dir = (
            workspace_root / f"enterprise_info_{tenant_id}" if tenant_id else workspace_root / "enterprise_info"
        )
        if enterprise_dir.exists():
            items.append("  📁 enterprise_info/ (shared company info)")

    dir_count = 0
    file_count = 0
    for item_path in sorted(target.iterdir()):
        if item_path.name.startswith("."):
            continue
        if item_path.is_dir():
            dir_count += 1
            child_count = len([child for child in item_path.iterdir() if not child.name.startswith(".")])
            items.append(f"  📁 {item_path.name}/ ({child_count} items)")
        elif item_path.is_file():
            file_count += 1
            size_bytes = item_path.stat().st_size
            size_str = f"{size_bytes}B" if size_bytes < 1024 else f"{size_bytes / 1024:.1f}KB"
            items.append(f"  📄 {item_path.name} ({size_str})")

    if not items:
        return f"📂 {rel_path or 'root'}: Empty directory (0 files, 0 folders)"
    header = f"📂 {rel_path or 'root'}: {dir_count} folder(s), {file_count} file(s)\n"
    return header + "\n".join(items)


def _read_file(
    ws: Path,
    rel_path: str,
    tenant_id: str | None = None,
    offset: int = 0,
    limit: int = 2000,
    *,
    resolve_tool_source_path,
) -> str:
    try:
        file_path = resolve_tool_source_path(ws, rel_path, tenant_id=tenant_id)
    except ValueError as error:
        return str(error)

    if not file_path.exists():
        return f"File not found: {rel_path}"

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        total_lines = len(lines)
        start = max(0, offset)
        end = min(total_lines, start + limit)
        if start >= total_lines:
            return f"Offset {offset} exceeds file length ({total_lines} lines total)"

        selected_lines = lines[start:end]
        output = "\n".join(f"{index + 1:6}\t{line}" for index, line in enumerate(selected_lines, start=start))
        if total_lines > end:
            output += f"\n\n... [{total_lines - end} more lines not shown, lines {end + 1}-{total_lines}]"
        header = f"📄 {rel_path} (lines {start + 1}-{end} of {total_lines})\n"
        return header + output
    except Exception as error:
        return f"Read failed: {error}"
