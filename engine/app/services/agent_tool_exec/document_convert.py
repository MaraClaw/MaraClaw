from __future__ import annotations

import uuid
from pathlib import Path

from app.core.logging import logger
from app.services.agent_tool_exec.registry import ToolArguments
from app.services.document_conversion.csv import _convert_csv_to_xlsx as _csv_to_xlsx
from app.services.document_conversion.markdown import (
    _convert_markdown_to_docx as _markdown_to_docx,
    _convert_markdown_to_pdf as _markdown_to_pdf,
)


def _resolve_paths(ws: Path, source_path: str, target_path: str) -> tuple[Path, Path] | str:
    from app.services import agent_tools

    try:
        return (
            agent_tools._resolve_tool_source_path(ws, source_path),
            agent_tools._resolve_tool_target_path(ws, target_path),
        )
    except ValueError as exc:
        return str(exc)


async def _convert_csv_to_xlsx(agent_id: uuid.UUID, ws: Path, arguments: ToolArguments) -> str:
    del agent_id
    source_path_value = arguments.get("source_path")
    target_path_value = arguments.get("target_path")
    source_path = source_path_value if isinstance(source_path_value, str) else ""
    target_path = target_path_value if isinstance(target_path_value, str) else ""
    if not source_path or not target_path:
        return "❌ Missing 'source_path' or 'target_path'."
    paths = _resolve_paths(ws, source_path, target_path)
    if isinstance(paths, str):
        return paths
    src_file, tgt_file = paths
    if not src_file.exists():
        return f"❌ Source file not found: {source_path}"
    try:
        return _csv_to_xlsx(src_file, tgt_file, target_path)
    except Exception as exc:
        logger.exception(f"Convert CSV to XLSX failed: {exc}")
        return f"❌ Conversion failed: {exc}"


async def _convert_html_to_pdf(agent_id: uuid.UUID, ws: Path, arguments: ToolArguments) -> str:
    del agent_id
    source_path_value = arguments.get("source_path")
    target_path_value = arguments.get("target_path")
    source_path = source_path_value if isinstance(source_path_value, str) else ""
    target_path = target_path_value if isinstance(target_path_value, str) else ""
    if not source_path or not target_path:
        return "❌ Missing 'source_path' or 'target_path'."
    paths = _resolve_paths(ws, source_path, target_path)
    if isinstance(paths, str):
        return paths
    src_file, tgt_file = paths
    if not src_file.exists():
        return f"❌ Source file not found: {source_path}"
    from app.services.document_conversion import convert_html_to_pdf

    return await convert_html_to_pdf(src_file, tgt_file, str(target_path), arguments)


async def _convert_html_to_pptx(agent_id: uuid.UUID, ws: Path, arguments: ToolArguments) -> str:
    del agent_id
    source_path_value = arguments.get("source_path")
    target_path_value = arguments.get("target_path")
    source_path = source_path_value if isinstance(source_path_value, str) else ""
    target_path = target_path_value if isinstance(target_path_value, str) else ""
    if not source_path or not target_path:
        return "❌ Missing paths."
    paths = _resolve_paths(ws, source_path, target_path)
    if isinstance(paths, str):
        return paths
    src_file, tgt_file = paths
    if not src_file.exists():
        return "❌ Source file not found."
    from app.services.document_conversion import convert_html_to_pptx

    return await convert_html_to_pptx(src_file, tgt_file, str(target_path), ws, arguments)


async def _convert_markdown_to_docx(agent_id: uuid.UUID, ws: Path, arguments: ToolArguments) -> str:
    del agent_id
    source_path_value = arguments.get("source_path")
    target_path_value = arguments.get("target_path")
    source_path = source_path_value if isinstance(source_path_value, str) else ""
    target_path = target_path_value if isinstance(target_path_value, str) else ""
    if not source_path or not target_path:
        return "❌ Missing paths."
    paths = _resolve_paths(ws, source_path, target_path)
    if isinstance(paths, str):
        return paths
    src_file, tgt_file = paths
    if not src_file.exists():
        return "❌ Source file not found."
    try:
        return _markdown_to_docx(src_file, tgt_file, target_path)
    except Exception as exc:
        logger.exception(f"Convert MD to Docx failed: {exc}")
        return f"❌ Conversion failed: {exc}"


async def _convert_markdown_to_pdf(agent_id: uuid.UUID, ws: Path, arguments: ToolArguments) -> str:
    del agent_id
    source_path_value = arguments.get("source_path")
    target_path_value = arguments.get("target_path")
    source_path = source_path_value if isinstance(source_path_value, str) else ""
    target_path = target_path_value if isinstance(target_path_value, str) else ""
    if not source_path or not target_path:
        return "❌ Missing paths."
    paths = _resolve_paths(ws, source_path, target_path)
    if isinstance(paths, str):
        return paths
    src_file, tgt_file = paths
    if not src_file.exists():
        return "❌ Source file not found."
    try:
        return _markdown_to_pdf(src_file, tgt_file, target_path, ws)
    except Exception as exc:
        logger.exception(f"Convert MD to PDF failed: {exc}")
        return f"❌ Conversion failed: {exc}"
