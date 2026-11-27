from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.config import get_settings
from app.core.logging import logger
from app.services.agent_tool_exec.registry import ToolArguments
from app.services.document_conversion import markdown as markdown_conversion
from app.services.document_conversion.conversion_lane import run_conversion, staged_output
from app.services.document_conversion.csv import _convert_csv_to_xlsx as _csv_to_xlsx


def _resolve_paths(ws: Path, source_path: str, target_path: str) -> tuple[Path, Path] | str:
    from app.services import agent_tools

    try:
        return (
            agent_tools._resolve_tool_source_path(ws, source_path),
            agent_tools._resolve_tool_target_path(ws, target_path),
        )
    except ValueError as exc:
        return str(exc)


async def _within_deadline(conversion: Awaitable[str]) -> str:
    """Await one conversion for at most `DOCUMENT_CONVERSION_TIMEOUT_SECONDS`; running out of time is the tool's error."""
    seconds = get_settings().DOCUMENT_CONVERSION_TIMEOUT_SECONDS
    deadline = asyncio.timeout(seconds)
    try:
        async with deadline:
            return await conversion
    except TimeoutError:
        if not deadline.expired():
            raise
        logger.warning(f"Document conversion passed its {seconds:g}s deadline")
        return f"❌ Conversion failed: it did not finish within {seconds:g}s. Try a smaller file."


async def _converted_into(tgt_file: Path, convert: Callable[[Path], Awaitable[str]]) -> str:
    """Run `convert` under its deadline, writing to a staged file that replaces `tgt_file` only if it succeeded.

    A conversion that times out, fails or is cancelled leaves `tgt_file` as it was, and its staged file is removed (when
    an abandoned lane thread is still running, as soon as that thread returns).
    """
    async with staged_output(tgt_file) as staged:
        result = await _within_deadline(convert(staged.path))
        if not result.startswith("✅"):
            return result
        try:
            staged.publish()
        except OSError as exc:
            logger.exception(f"Publishing the converted file failed: {exc}")
            return f"❌ Conversion failed: {exc}"
        return result


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
        return await _converted_into(
            tgt_file, lambda staged: run_conversion(_csv_to_xlsx, src_file, staged, target_path)
        )
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

    return await _converted_into(
        tgt_file, lambda staged: convert_html_to_pdf(src_file, staged, str(target_path), ws, arguments)
    )


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

    return await _converted_into(
        tgt_file, lambda staged: convert_html_to_pptx(src_file, staged, str(target_path), ws, arguments)
    )


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
        return await _converted_into(
            tgt_file,
            lambda staged: run_conversion(markdown_conversion._convert_markdown_to_docx, src_file, staged, target_path),
        )
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
        return await _converted_into(
            tgt_file,
            lambda staged: run_conversion(
                markdown_conversion._convert_markdown_to_pdf, src_file, staged, target_path, ws
            ),
        )
    except Exception as exc:
        logger.exception(f"Convert MD to PDF failed: {exc}")
        return f"❌ Conversion failed: {exc}"
