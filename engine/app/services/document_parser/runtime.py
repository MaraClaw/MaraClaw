"""Run untrusted anydoc conversion off the event loop with hard bounds."""

from __future__ import annotations

import asyncio
import contextlib
import json
import multiprocessing as mp
import os
import signal
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import BoundedSemaphore
from typing import Final, Protocol, cast

from app.core.json_types import json_loads_object
from app.core.logging import logger
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
    extract_xlsx_sheets,
    reconstruct_parse_error,
)

TIMEOUT_SECONDS: Final = 25
SLOT_WAIT_SECONDS: Final = 5.0
MAX_CONCURRENCY: Final = 2
_CHILD_AS_BYTES: Final = 768 * 1024 * 1024
_CHILD_FSIZE_BYTES: Final = 64 * 1024 * 1024
_CONVERT_PERMITS = BoundedSemaphore(MAX_CONCURRENCY)


class _ProcessLike(Protocol):
    pid: int | None

    def start(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


def convert_document_isolated(
    data: bytes,
    filename: str,
    *,
    max_output_chars: int | None = None,
    timeout_seconds: float = TIMEOUT_SECONDS,
    slot_wait_seconds: float = SLOT_WAIT_SECONDS,
) -> str:
    """Convert office bytes in a killable child process.

    Bounds input size, output length, wall-clock time, and concurrent workers.
    Slot wait and parse use separate budgets. Temporary files are always removed.
    """
    return _run_isolated(
        data,
        filename,
        convert_worker,
        max_output_chars,
        timeout_seconds=timeout_seconds,
        slot_wait_seconds=slot_wait_seconds,
    )


async def convert_document_isolated_async(
    data: bytes,
    filename: str,
    *,
    max_output_chars: int | None = None,
    timeout_seconds: float = TIMEOUT_SECONDS,
    slot_wait_seconds: float = SLOT_WAIT_SECONDS,
) -> str:
    """Async wrapper that keeps binary parsing off the event loop."""
    return await asyncio.to_thread(
        convert_document_isolated,
        data,
        filename,
        max_output_chars=max_output_chars,
        timeout_seconds=timeout_seconds,
        slot_wait_seconds=slot_wait_seconds,
    )


def extract_xlsx_sheets_isolated(
    data: bytes,
    filename: str,
    *,
    timeout_seconds: float = TIMEOUT_SECONDS,
    slot_wait_seconds: float = SLOT_WAIT_SECONDS,
) -> list[dict[str, object]]:
    """Run openpyxl sheet extraction in the same isolated worker pool."""
    raw = _run_isolated(
        data,
        filename,
        xlsx_sheets_worker,
        None,
        timeout_seconds=timeout_seconds,
        slot_wait_seconds=slot_wait_seconds,
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DocumentParseError("Document read failed: extractor returned unreadable sheets") from exc
    if not isinstance(payload, list):
        raise DocumentParseError("Document read failed: extractor returned unreadable sheets")
    return [item for item in payload if isinstance(item, dict)]


async def extract_xlsx_sheets_isolated_async(
    data: bytes,
    filename: str,
    *,
    timeout_seconds: float = TIMEOUT_SECONDS,
    slot_wait_seconds: float = SLOT_WAIT_SECONDS,
) -> list[dict[str, object]]:
    return await asyncio.to_thread(
        extract_xlsx_sheets_isolated,
        data,
        filename,
        timeout_seconds=timeout_seconds,
        slot_wait_seconds=slot_wait_seconds,
    )


def convert_worker(
    src_path: str,
    filename: str,
    out_path: str,
    err_path: str,
    max_output_chars: int | None,
) -> None:
    """Spawn entrypoint. Must stay a module-level function so it pickles."""
    _harden_child()
    try:
        data = Path(src_path).read_bytes()
        markdown = convert_document(data, filename, max_output_chars=max_output_chars)
        _ = Path(out_path).write_text(markdown, encoding="utf-8")
    except BaseException as exc:
        _write_worker_error(err_path, exc)


def xlsx_sheets_worker(
    src_path: str,
    filename: str,
    out_path: str,
    err_path: str,
    _: object,
) -> None:
    """Spawn entrypoint for structured XLSX preview. Must stay module-level."""
    del filename
    _harden_child()
    try:
        sheets = extract_xlsx_sheets(Path(src_path).read_bytes())
        _ = Path(out_path).write_text(json.dumps(sheets), encoding="utf-8")
    except BaseException as exc:
        _write_worker_error(err_path, exc)


def _run_isolated(
    data: bytes,
    filename: str,
    worker: Callable[[str, str, str, str, int | None], None],
    extra: int | None,
    *,
    timeout_seconds: float,
    slot_wait_seconds: float,
) -> str:
    if len(data) > MAX_INPUT_BYTES:
        raise DocumentTooLargeError(
            f"Document is too large to read safely ({len(data) / 1024 / 1024:.1f} MB).",
            size_bytes=len(data),
        )
    acquired = _CONVERT_PERMITS.acquire(timeout=slot_wait_seconds)
    if not acquired:
        raise DocumentParseTimeoutError(
            f"Document parse timed out after {slot_wait_seconds:.0f}s waiting for a parser slot."
        )
    reaped = True
    try:
        return _spawn_job(
            data,
            filename,
            worker,
            extra,
            timeout_seconds=timeout_seconds,
        )
    except DocumentParseTimeoutError as exc:
        reaped = exc.child_reaped
        raise
    finally:
        if reaped:
            _CONVERT_PERMITS.release()


def _spawn_job(
    data: bytes,
    filename: str,
    worker: Callable[[str, str, str, str, int | None], None],
    extra: int | None,
    *,
    timeout_seconds: float,
) -> str:
    tmp = tempfile.TemporaryDirectory(prefix="anydoc-")
    try:
        root = Path(tmp.name)
        src = root / "input.bin"
        out = root / "output.md"
        err = root / "error.json"
        try:
            _ = src.write_bytes(data)
        except OSError as exc:
            raise DocumentParseError(f"Document read failed: {exc}") from exc
        ctx = mp.get_context("spawn")
        spawned = cast(
            _ProcessLike,
            ctx.Process(
                target=worker,
                args=(str(src), filename, str(out), str(err), extra),
                daemon=True,
            ),
        )
        spawned.start()
        spawned.join(timeout_seconds)
        if spawned.is_alive():
            child_reaped = _kill_process(spawned)
            raise DocumentParseTimeoutError(
                f"Document parse timed out after {timeout_seconds:.0f}s. "
                + "The file may be too large or too complex to extract safely.",
                child_reaped=child_reaped,
            )
        try:
            if err.exists():
                raise _error_from_payload(err.read_text(encoding="utf-8"))
            if out.exists():
                return out.read_text(encoding="utf-8")
        except OSError as exc:
            raise DocumentParseError(f"Document read failed: {exc}") from exc
        exitcode = getattr(spawned, "exitcode", None)
        if exitcode:
            raise DocumentParseError(f"Document read failed: extractor exited with code {exitcode}")
        raise DocumentParseError("Document read failed: extractor returned no content")
    finally:
        with contextlib.suppress(OSError):
            tmp.cleanup()


def _kill_process(spawned: _ProcessLike) -> bool:
    pid = spawned.pid
    if isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            spawned.terminate()
    else:
        spawned.terminate()
    spawned.join(2)
    if spawned.is_alive():
        if isinstance(pid, int) and pid > 0:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                spawned.kill()
        else:
            spawned.kill()
        spawned.join(1)
    return not spawned.is_alive()


def _harden_child() -> None:
    with contextlib.suppress(OSError):
        os.setsid()
    try:
        import resource

        cpu = max(1, int(TIMEOUT_SECONDS) + 5)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_AS, (_CHILD_AS_BYTES, _CHILD_AS_BYTES))
        resource.setrlimit(resource.RLIMIT_FSIZE, (_CHILD_FSIZE_BYTES, _CHILD_FSIZE_BYTES))
        if hasattr(resource, "RLIMIT_CORE"):
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception as exc:
        logger.warning(f"[DocumentParser] Failed to apply child resource limits: {exc}")


def _write_worker_error(err_path: str, exc: BaseException) -> None:
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
    try:
        _ = Path(err_path).write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        raise SystemExit(1) from exc


def _error_from_payload(raw: str) -> DocumentParseError:
    try:
        payload = json_loads_object(raw)
    except Exception:
        return DocumentParseError("Document read failed: extractor returned unreadable error")
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
