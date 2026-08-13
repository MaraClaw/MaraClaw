from __future__ import annotations

import re
from pathlib import Path


def _search_files(
    ws: Path,
    pattern: str,
    path: str = ".",
    file_pattern: str = "*",
    ignore_case: bool = False,
    tenant_id: str | None = None,
    *,
    workspace_root: Path,
) -> str:
    if path and path.startswith("enterprise_info"):
        enterprise_root = (
            (workspace_root / f"enterprise_info_{tenant_id}").resolve()
            if tenant_id
            else (workspace_root / "enterprise_info").resolve()
        )
        sub = path[len("enterprise_info") :].lstrip("/")
        search_path = (enterprise_root / sub).resolve() if sub else enterprise_root
        if not search_path.is_relative_to(enterprise_root):
            return "Access denied for this path"
        ws_for_relative = enterprise_root
    else:
        search_path = (ws / path).resolve() if path and path != "." else ws
        if not search_path.is_relative_to(ws.resolve()):
            return "Access denied for this path"
        ws_for_relative = ws

    if not search_path.exists():
        return f"Directory not found: {path}"

    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as error:
        return f"Invalid regex pattern: {error}"

    results = []
    total_matches = 0
    files_searched = 0
    for file_path in search_path.rglob(file_pattern):
        if not file_path.is_file():
            continue
        if file_path.name.startswith("."):
            continue
        if file_path.suffix.lower() in {
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
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            content = ""
        for index, line in enumerate(content.splitlines(), 1):
            if regex.search(line):
                rel_path = file_path.relative_to(ws_for_relative)
                results.append(f"{rel_path}:{index}: {line.strip()[:100]}")
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
    header = (
        f"🔍 Found {total_matches}+ match(es) in {files_searched} file(s) for pattern '{pattern}'{truncation_note}:\n"
    )
    return header + "\n".join(results)


def _find_files(
    ws: Path,
    pattern: str,
    path: str = ".",
    tenant_id: str | None = None,
    *,
    workspace_root: Path,
) -> str:
    if path and path.startswith("enterprise_info"):
        enterprise_root = (
            (workspace_root / f"enterprise_info_{tenant_id}").resolve()
            if tenant_id
            else (workspace_root / "enterprise_info").resolve()
        )
        sub = path[len("enterprise_info") :].lstrip("/")
        search_path = (enterprise_root / sub).resolve() if sub else enterprise_root
        if not search_path.is_relative_to(enterprise_root):
            return "Access denied for this path"
        ws_for_relative = enterprise_root
    else:
        search_path = (ws / path).resolve() if path and path != "." else ws
        if not search_path.is_relative_to(ws.resolve()):
            return "Access denied for this path"
        ws_for_relative = ws

    if not search_path.exists():
        return f"Directory not found: {path}"

    try:
        matches = list(search_path.glob(pattern))
    except Exception as error:
        return f"Invalid glob pattern: {error}"

    if not matches:
        return f"No files matching pattern: {pattern}"

    matches.sort(key=lambda item_path: item_path.stat().st_mtime if item_path.exists() else 0, reverse=True)
    results = []
    dir_count = 0
    file_count = 0
    for match in matches[:100]:
        rel_path = match.relative_to(ws_for_relative)
        if match.is_dir():
            dir_count += 1
            results.append(f"📁 {rel_path}/")
        else:
            file_count += 1
            try:
                size = match.stat().st_size
                size_str = f"{size // 1024}KB" if size > 1024 else f"{size}B"
                results.append(f"📄 {rel_path} ({size_str})")
            except Exception:
                results.append(f"📄 {rel_path}")

    header = f"📂 Found {len(matches)} item(s) ({dir_count} dirs, {file_count} files) matching '{pattern}':\n"
    return header + "\n".join(results)
