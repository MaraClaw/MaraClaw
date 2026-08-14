from __future__ import annotations

import asyncio
import importlib
import multiprocessing as mp
import queue
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, TypeIs

from . import document_reading


class _FitzPage(Protocol):
    def get_text(self, kind: str) -> object: ...


class _FitzDocument(Protocol):
    def __enter__(self) -> Sequence[_FitzPage]: ...

    def __exit__(self, *args: object) -> object: ...

    def __getitem__(self, key: slice) -> Sequence[_FitzPage]: ...


class _FitzModule(Protocol):
    def open(self, path: str) -> _FitzDocument: ...


def _is_fitz_module(value: object) -> TypeIs[_FitzModule]:
    return callable(getattr(value, "open", None))


_READ_DOCUMENT_TIMEOUT_SECONDS = 25
_READ_DOCUMENT_FALLBACK_TIMEOUT_SECONDS = 10


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


def _read_pdf_fast_sync(ws: Path, rel_path: str, max_chars: int = 8000, tenant_id: str | None = None) -> str:
    """Fast PDF text extraction fallback for files that make pdfplumber/pdfminer hang."""
    from app.services import agent_tools

    max_chars = min(max(int(max_chars), 1), 20000)
    try:
        file_path = agent_tools._resolve_tool_source_path(ws, rel_path, tenant_id=tenant_id)
    except ValueError as exc:
        return str(exc)

    if not file_path.exists():
        return f"File not found: {rel_path}"
    if file_path.is_dir():
        return f"Path is a directory, not a document: {rel_path}"
    try:
        fitz_mod: object = importlib.import_module("fitz")
        if not _is_fitz_module(fitz_mod):
            return "PDF fallback extractor unavailable: fitz.open is missing"

        text_parts: list[str] = []
        with fitz_mod.open(str(file_path)) as doc:
            for index, page in enumerate(doc[:50]):
                page_text_raw = page.get_text("text")
                page_text = page_text_raw if isinstance(page_text_raw, str) else ""
                if page_text:
                    text_parts.append(f"--- Page {index + 1} ---\n{page_text}")
                if sum(len(part) for part in text_parts) >= max_chars:
                    break
        content = "\n\n".join(text_parts) if text_parts else "(PDF is empty or text extraction failed)"
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n...[truncated, {len(content)} chars total]"
        return content
    except ImportError as exc:
        return f"PDF fallback extractor unavailable: {exc}. Install: pip install PyMuPDF"
    except Exception as exc:
        return f"PDF fallback extraction failed: {str(exc)[:200]}"


def _read_pdf_fast_worker(
    out_queue: mp.Queue[tuple[str, str]],
    ws_str: str,
    rel_path: str,
    max_chars: int,
    tenant_id: str | None,
) -> None:
    try:
        out_queue.put(("ok", _read_pdf_fast_sync(Path(ws_str), rel_path, max_chars=max_chars, tenant_id=tenant_id)))
    except BaseException as exc:
        out_queue.put(("error", f"PDF fallback extraction failed: {str(exc)[:200]}"))


def _read_pdf_fast_with_timeout(ws: Path, rel_path: str, max_chars: int = 8000, tenant_id: str | None = None) -> str:
    ctx = mp.get_context("spawn")
    out_queue: mp.Queue[tuple[str, str]] = ctx.Queue(maxsize=1)
    proc = ctx.Process(
        target=_read_pdf_fast_worker,
        args=(out_queue, str(ws), rel_path, max_chars, tenant_id),
        daemon=True,
    )
    proc.start()
    proc.join(_READ_DOCUMENT_FALLBACK_TIMEOUT_SECONDS)
    if proc.is_alive():
        proc.terminate()
        proc.join(2)
        if proc.is_alive():
            proc.kill()
            proc.join(1)
        return (
            f"Document read timed out after {_READ_DOCUMENT_TIMEOUT_SECONDS}s, "
            + f"and PDF fallback also timed out after {_READ_DOCUMENT_FALLBACK_TIMEOUT_SECONDS}s. "
            + "The file may be too large or too complex to extract safely."
        )
    try:
        status, payload = out_queue.get_nowait()
    except queue.Empty:
        if proc.exitcode:
            return f"PDF fallback extraction failed: extractor exited with code {proc.exitcode}"
        return "PDF fallback extraction failed: extractor returned no content"
    if status == "ok":
        return payload
    return str(payload)


def _read_document_with_timeout(ws: Path, rel_path: str, max_chars: int = 8000, tenant_id: str | None = None) -> str:
    """Run document parsing in a killable child process so one bad file cannot freeze the site."""
    ctx = mp.get_context("spawn")
    out_queue: mp.Queue[tuple[str, str]] = ctx.Queue(maxsize=1)
    proc = ctx.Process(
        target=_read_document_worker,
        args=(out_queue, str(ws), rel_path, max_chars, tenant_id),
        daemon=True,
    )
    proc.start()
    proc.join(_READ_DOCUMENT_TIMEOUT_SECONDS)
    if proc.is_alive():
        proc.terminate()
        proc.join(2)
        if proc.is_alive():
            proc.kill()
            proc.join(1)
        if Path(rel_path).suffix.lower() == ".pdf":
            return _read_pdf_fast_with_timeout(ws, rel_path, max_chars=max_chars, tenant_id=tenant_id)
        return (
            f"Document read timed out after {_READ_DOCUMENT_TIMEOUT_SECONDS}s. "
            + "The file may be too large or too complex to extract safely. "
            + "Please split it, convert it to text/Markdown, or read a smaller excerpt."
        )
    try:
        status, payload = out_queue.get_nowait()
    except queue.Empty:
        if proc.exitcode:
            return f"Document read failed: extractor exited with code {proc.exitcode}"
        return "Document read failed: extractor returned no content"
    if status == "ok":
        return payload
    return str(payload)


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
