from __future__ import annotations

import asyncio
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
    return await asyncio.to_thread(_read_document_with_timeout, ws, rel_path, max_chars, tenant_id)


async def _read_document_from_storage(
    agent_id: uuid.UUID,
    rel_path: str,
    max_chars: int = 8000,
    tenant_id: str | None = None,
) -> str:
    from app.services import agent_tools

    temp_workspace = await agent_tools._prepare_temp_workspace(agent_id, tenant_id=tenant_id, paths=[rel_path])
    try:
        return await _read_document(temp_workspace.root, rel_path, max_chars=max_chars, tenant_id=None)
    finally:
        temp_workspace.cleanup()
