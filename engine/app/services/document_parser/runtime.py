"""Run untrusted anydoc conversion off the event loop with hard bounds."""

from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import tempfile
from pathlib import Path
from threading import BoundedSemaphore
from typing import Final

from app.core.json_types import json_loads_object
from app.services.document_parser.errors import (
    DocumentParseError,
    DocumentParseTimeoutError,
    DocumentTooLargeError,
    MalformedDocumentError,
    MissingDocumentPartError,
    ResourceLimitedDocumentError,
)
from app.services.document_parser.service import (
    MAX_INPUT_BYTES,
    convert_document,
    reconstruct_parse_error,
)

TIMEOUT_SECONDS: Final = 25
MAX_CONCURRENCY: Final = 2
_CONVERT_PERMITS = BoundedSemaphore(MAX_CONCURRENCY)


def convert_document_isolated(
    data: bytes,
    filename: str,
    *,
    max_output_chars: int | None = None,
    timeout_seconds: float = TIMEOUT_SECONDS,
) -> str:
    """Convert office bytes in a killable child process.

    Bounds input size, output length, wall-clock time, and concurrent workers.
    Temporary files are always removed.
    """
    if len(data) > MAX_INPUT_BYTES:
        raise DocumentTooLargeError(
            f"Document is too large to read safely ({len(data) / 1024 / 1024:.1f} MB).",
            size_bytes=len(data),
        )
    acquired = _CONVERT_PERMITS.acquire(timeout=timeout_seconds)
    if not acquired:
        raise DocumentParseTimeoutError(
            f"Document parse timed out after {timeout_seconds:.0f}s waiting for a parser slot."
        )
    try:
        return _spawn_conversion(data, filename, max_output_chars=max_output_chars, timeout_seconds=timeout_seconds)
    finally:
        _CONVERT_PERMITS.release()


async def convert_document_isolated_async(
    data: bytes,
    filename: str,
    *,
    max_output_chars: int | None = None,
    timeout_seconds: float = TIMEOUT_SECONDS,
) -> str:
    """Async wrapper that keeps binary parsing off the event loop."""
    return await asyncio.to_thread(
        convert_document_isolated,
        data,
        filename,
        max_output_chars=max_output_chars,
        timeout_seconds=timeout_seconds,
    )


def convert_worker(
    src_path: str,
    filename: str,
    out_path: str,
    err_path: str,
    max_output_chars: int | None,
) -> None:
    """Spawn entrypoint. Must stay a module-level function so it pickles."""
    try:
        data = Path(src_path).read_bytes()
        markdown = convert_document(data, filename, max_output_chars=max_output_chars)
        _ = Path(out_path).write_text(markdown, encoding="utf-8")
    except BaseException as exc:
        part = exc.part if isinstance(exc, (MalformedDocumentError, MissingDocumentPartError)) else None
        limit = exc.limit if isinstance(exc, ResourceLimitedDocumentError) else None
        size_bytes = exc.size_bytes if isinstance(exc, DocumentTooLargeError) else None
        payload = {
            "type": type(exc).__name__,
            "message": str(exc)[:2000],
            "part": part,
            "limit": limit,
            "size_bytes": size_bytes,
        }
        _ = Path(err_path).write_text(json.dumps(payload), encoding="utf-8")


def _spawn_conversion(
    data: bytes,
    filename: str,
    *,
    max_output_chars: int | None,
    timeout_seconds: float,
) -> str:
    tmp = tempfile.TemporaryDirectory(prefix="anydoc-")
    try:
        root = Path(tmp.name)
        src = root / "input.bin"
        out = root / "output.md"
        err = root / "error.json"
        _ = src.write_bytes(data)
        ctx = mp.get_context("spawn")
        spawned = ctx.Process(
            target=convert_worker,
            args=(str(src), filename, str(out), str(err), max_output_chars),
            daemon=True,
        )
        spawned.start()
        spawned.join(timeout_seconds)
        if spawned.is_alive():
            spawned.terminate()
            spawned.join(2)
            if spawned.is_alive():
                spawned.kill()
                spawned.join(1)
            raise DocumentParseTimeoutError(
                f"Document parse timed out after {timeout_seconds:.0f}s. "
                + "The file may be too large or too complex to extract safely."
            )
        if err.exists():
            raise _error_from_payload(err.read_text(encoding="utf-8"))
        if out.exists():
            return out.read_text(encoding="utf-8")
        if spawned.exitcode:
            raise DocumentParseError(f"Document read failed: extractor exited with code {spawned.exitcode}")
        raise DocumentParseError("Document read failed: extractor returned no content")
    finally:
        tmp.cleanup()


def _error_from_payload(raw: str) -> DocumentParseError:
    payload = json_loads_object(raw)
    if not payload:
        return DocumentParseError("Document read failed: extractor returned unreadable error")
    type_name = payload.get("type")
    message = payload.get("message")
    part = payload.get("part")
    limit = payload.get("limit")
    size_bytes = payload.get("size_bytes")
    return reconstruct_parse_error(
        type_name if isinstance(type_name, str) else DocumentParseError.__name__,
        message if isinstance(message, str) else "Document read failed",
        part=part if isinstance(part, str) else None,
        limit=limit if isinstance(limit, str) else None,
        size_bytes=size_bytes if isinstance(size_bytes, int) else None,
    )
