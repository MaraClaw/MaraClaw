from __future__ import annotations

import shutil
from pathlib import Path


def _write_file(
    ws: Path,
    rel_path: str,
    content: str,
    tenant_id: str | None = None,
    *,
    workspace_root: Path,
    is_enterprise_info_path,
) -> str:
    if rel_path.strip("/") == "tasks.json":
        return "tasks.json is a legacy read-only snapshot. Use the task APIs/UI to manage tasks."
    if is_enterprise_info_path(rel_path):
        return "enterprise_info is shared company context and is read-only for agents. Ask an admin to update it."

    if rel_path and rel_path.startswith("enterprise_info"):
        enterprise_root = (
            (workspace_root / f"enterprise_info_{tenant_id}").resolve()
            if tenant_id
            else (workspace_root / "enterprise_info").resolve()
        )
        sub = rel_path[len("enterprise_info") :].lstrip("/")
        if not sub:
            return "Write failed: please provide a file path under enterprise_info/, e.g. enterprise_info/knowledge_base/report.md"
        file_path = (enterprise_root / sub).resolve()
        if not file_path.is_relative_to(enterprise_root):
            return "Access denied for this path"
    else:
        file_path = (ws / rel_path).resolve()
        if not file_path.is_relative_to(ws.resolve()):
            return "Access denied for this path"

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"✅ Written to {rel_path} ({len(content)} chars)"
    except Exception as error:
        return f"Write failed: {error}"


def _delete_file(ws: Path, rel_path: str, *, is_enterprise_info_path) -> str:
    protected = {"tasks.json", "soul.md"}
    if rel_path.strip("/") in protected:
        return f"{rel_path} cannot be deleted (protected)"
    if is_enterprise_info_path(rel_path):
        return "enterprise_info is shared company context and is read-only for agents. Ask an admin to update it."

    file_path = (ws / rel_path).resolve()
    if not file_path.is_relative_to(ws.resolve()):
        return "Access denied for this path"
    if not file_path.exists():
        return f"File not found: {rel_path}"

    try:
        if file_path.is_dir():
            shutil.rmtree(file_path)
            return f"✅ Deleted directory {rel_path}"
        file_path.unlink()
        return f"✅ Deleted {rel_path}"
    except Exception as error:
        return f"Delete failed: {error}"


def _edit_file(
    ws: Path,
    rel_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    tenant_id: str | None = None,
    *,
    workspace_root: Path,
    is_enterprise_info_path,
) -> str:
    if is_enterprise_info_path(rel_path):
        return "enterprise_info is shared company context and is read-only for agents. Ask an admin to update it."

    if rel_path and rel_path.startswith("enterprise_info"):
        enterprise_root = (
            (workspace_root / f"enterprise_info_{tenant_id}").resolve()
            if tenant_id
            else (workspace_root / "enterprise_info").resolve()
        )
        sub = rel_path[len("enterprise_info") :].lstrip("/")
        file_path = (enterprise_root / sub).resolve() if sub else enterprise_root
        if not file_path.is_relative_to(enterprise_root):
            return "Access denied for this path"
    else:
        file_path = (ws / rel_path).resolve()
        if not file_path.is_relative_to(ws.resolve()):
            return "Access denied for this path"

    if not file_path.exists():
        return f"File not found: {rel_path}"
    if not file_path.is_file():
        return f"Not a file: {rel_path}"

    try:
        content = file_path.read_text(encoding="utf-8")
        if old_string not in content:
            return f"❌ 'old_string' not found in {rel_path}. Please check the exact text including whitespace and newlines."

        if replace_all:
            new_content = content.replace(old_string, new_string)
            count = content.count(old_string)
        else:
            count = content.count(old_string)
            if count > 1:
                return f"❌ 'old_string' appears {count} times in {rel_path}. Use replace_all=true or provide more context to make the match unique."
            new_content = content.replace(old_string, new_string, 1)
            count = 1

        file_path.write_text(new_content, encoding="utf-8")
        return f"✅ Replaced {count} occurrence(s) in {rel_path}"
    except Exception as error:
        return f"Edit failed: {error}"
