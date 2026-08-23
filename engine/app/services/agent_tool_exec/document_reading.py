from __future__ import annotations

from pathlib import Path

from app.services.document_parser import (
    DocumentParseError,
    DocumentTooLargeError,
    convert_document_isolated,
    decode_text_bytes,
    format_parse_failure,
    is_plain_text_document,
    needs_extraction,
    unsupported_read_document_message,
)

_READ_DOCUMENT_MAX_CHARS = 20000
_EMPTY_DOCUMENT = "(Document is empty or uses unsupported formatting)"


def _truncate_document_text(content: str, max_chars: int) -> str:
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + f"\n\n...[truncated, {len(content)} chars total]"


def _read_document_sync(ws: Path, rel_path: str, max_chars: int = 8000, tenant_id: str | None = None) -> str:
    """Synchronous document extraction. Must run outside the uvicorn event loop."""
    from app.services import agent_tools

    max_chars = min(max(int(max_chars), 1), _READ_DOCUMENT_MAX_CHARS)
    try:
        file_path = agent_tools._resolve_tool_source_path(ws, rel_path, tenant_id=tenant_id)
    except ValueError as exc:
        return str(exc)

    if not file_path.exists():
        return f"File not found: {rel_path}"
    if file_path.is_dir():
        return f"Path is a directory, not a document: {rel_path}"

    try:
        file_size = file_path.stat().st_size
    except OSError:
        file_size = 0
    if file_size > 50 * 1024 * 1024:
        return format_parse_failure(DocumentTooLargeError("too large", size_bytes=file_size))

    if is_plain_text_document(file_path.name) or file_path.suffix.lower() in {".txt", ".md", ".json", ".csv", ".log"}:
        try:
            content = decode_text_bytes(file_path.read_bytes())
        except OSError as exc:
            return f"Document read failed: {str(exc)[:200]}"
        return _truncate_document_text(content, max_chars)

    if not needs_extraction(file_path.name):
        return unsupported_read_document_message(file_path.suffix.lower() or file_path.name)

    try:
        content = convert_document_isolated(
            file_path.read_bytes(),
            file_path.name,
            max_output_chars=max_chars,
        )
    except DocumentParseError as exc:
        return format_parse_failure(exc)
    except OSError as exc:
        return f"Document read failed: {str(exc)[:200]}"

    if not content.strip():
        return _EMPTY_DOCUMENT
    return content
