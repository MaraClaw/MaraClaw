from __future__ import annotations

import multiprocessing as mp
import uuid
from pathlib import Path

from . import document_reading


def _read_document_worker(
    out_queue: mp.Queue[tuple[str, str]],
    ws_str: str,
    rel_path: str,
    max_chars: int,
    tenant_id: str | None,
) -> None:
    try:
        out_queue.put(
            (
                "ok",
                document_reading._read_document_sync(Path(ws_str), rel_path, max_chars=max_chars, tenant_id=tenant_id),
            )
        )
    except BaseException as exc:
        out_queue.put(("error", f"Document read failed: {str(exc)[:200]}"))


def _read_document_with_timeout(ws: Path, rel_path: str, max_chars: int = 8000, tenant_id: str | None = None) -> str:
    """Parse in the shared isolated anydoc runtime (timeout/bounds live there)."""
    return document_reading._read_document_sync(ws, rel_path, max_chars=max_chars, tenant_id=tenant_id)


async def _read_document(ws: Path, rel_path: str, max_chars: int = 8000, tenant_id: str | None = None) -> str:
    """Read content from office documents (PDF, DOCX, XLSX, PPTX)."""
    return await document_reading._read_document_async(ws, rel_path, max_chars, tenant_id)


async def _read_document_from_storage(
    agent_id: uuid.UUID,
    rel_path: str,
    max_chars: int = 8000,
    tenant_id: str | None = None,
) -> str:
    from app.services import agent_tools
    from app.services.document_parser import MAX_INPUT_BYTES, DocumentTooLargeError, format_parse_failure
    from app.services.storage import get_storage_backend
    from app.services.storage_runtime.base import StorageReadLimitExceededError

    storage_key, normalized, is_enterprise = agent_tools._tool_storage_key(agent_id, rel_path, tenant_id)
    if is_enterprise and tenant_id in (None, ""):
        return f"File not found: {rel_path}"

    storage = get_storage_backend()
    if await storage.is_dir(storage_key):
        return f"Path is a directory, not a document: {rel_path}"
    if not await storage.is_file(storage_key):
        return f"File not found: {rel_path}"

    try:
        data = await storage.read_bytes_bounded(storage_key, MAX_INPUT_BYTES)
    except StorageReadLimitExceededError as exc:
        return format_parse_failure(DocumentTooLargeError("too large", size_bytes=exc.size_bytes))
    filename = Path(normalized).name or Path(rel_path).name
    return await document_reading._read_document_from_bytes_async(data, filename, max_chars)
