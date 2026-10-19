"""Run untrusted anydoc conversion off the event loop with hard bounds."""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing as mp
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from threading import BoundedSemaphore, Event
from typing import Final, cast

from app.services.document_parser.child_workers import (
    TIMEOUT_SECONDS,
    convert_worker,
    decode_sheets,
    decode_worker_error as _error_from_payload,
    xlsx_sheets_worker,
)
from app.services.document_parser.errors import (
    DocumentParseError,
    DocumentParseTimeoutError,
    DocumentTooLargeError,
)
from app.services.document_parser.service import MAX_INPUT_BYTES
from app.services.document_parser.worker_cleanup import ProcessLike, kill_process

SLOT_WAIT_SECONDS: Final = 5.0
MAX_CONCURRENCY: Final = 2
_CONVERT_PERMITS = BoundedSemaphore(MAX_CONCURRENCY)
_QUARANTINED_JOBS: list[tuple[ProcessLike, tempfile.TemporaryDirectory[str]]] = []


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
    Slot wait and parse use separate budgets. Unreaped workers quarantine capacity.
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
    return await _run_owned_async(
        data,
        filename,
        convert_worker,
        max_output_chars,
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
    return decode_sheets(raw)


async def extract_xlsx_sheets_isolated_async(
    data: bytes,
    filename: str,
    *,
    timeout_seconds: float = TIMEOUT_SECONDS,
    slot_wait_seconds: float = SLOT_WAIT_SECONDS,
) -> list[dict[str, object]]:
    raw = await _run_owned_async(
        data,
        filename,
        xlsx_sheets_worker,
        None,
        timeout_seconds=timeout_seconds,
        slot_wait_seconds=slot_wait_seconds,
    )
    return decode_sheets(raw)


async def _run_owned_async(
    data: bytes,
    filename: str,
    worker: Callable[[str, str, str, str, int | None], None],
    extra: int | None,
    *,
    timeout_seconds: float,
    slot_wait_seconds: float,
) -> str:
    stopped = Event()
    owner = asyncio.create_task(
        asyncio.to_thread(
            _run_isolated,
            data,
            filename,
            worker,
            extra,
            timeout_seconds=timeout_seconds,
            slot_wait_seconds=slot_wait_seconds,
            stopped=stopped,
        )
    )
    try:
        return await asyncio.shield(owner)
    except asyncio.CancelledError:
        stopped.set()
        while not owner.done():
            try:
                await asyncio.shield(owner)
            except asyncio.CancelledError:
                continue
            except DocumentParseError:
                break
        with contextlib.suppress(DocumentParseError):
            owner.result()
        raise


def _run_isolated(
    data: bytes,
    filename: str,
    worker: Callable[[str, str, str, str, int | None], None],
    extra: int | None,
    *,
    timeout_seconds: float,
    slot_wait_seconds: float,
    stopped: Event | None = None,
) -> str:
    if len(data) > MAX_INPUT_BYTES:
        raise DocumentTooLargeError(
            f"Document is too large to read safely ({len(data) / 1024 / 1024:.1f} MB).",
            size_bytes=len(data),
        )
    stop = stopped if stopped is not None else Event()
    deadline = time.monotonic() + slot_wait_seconds
    while not stop.is_set():
        if _CONVERT_PERMITS.acquire(blocking=False):
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DocumentParseTimeoutError(
                f"Document parse timed out after {slot_wait_seconds:.0f}s waiting for a parser slot."
            )
        _ = stop.wait(min(0.01, remaining))
    else:
        return ""
    reaped = True
    try:
        return _spawn_job(
            data,
            filename,
            worker,
            extra,
            timeout_seconds=timeout_seconds,
            stopped=stop,
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
    stopped: Event,
) -> str:
    tmp: tempfile.TemporaryDirectory[str] | None = None
    spawned: ProcessLike | None = None
    started = False
    child_reaped = True
    try:
        if stopped.is_set():
            return ""
        tmp = tempfile.TemporaryDirectory(prefix="anydoc-")
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
            ProcessLike,
            ctx.Process(
                target=worker,
                args=(str(src), filename, str(out), str(err), extra),
                daemon=True,
            ),
        )
        if stopped.is_set():
            return ""
        spawned.start()
        started = True
        deadline = time.monotonic() + timeout_seconds
        while spawned.is_alive():
            if stopped.is_set():
                return ""
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DocumentParseTimeoutError(
                    f"Document parse timed out after {timeout_seconds:.0f}s. "
                    + "The file may be too large or too complex to extract safely."
                )
            spawned.join(min(0.02, remaining))
        spawned.join(0)
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
    except (OSError, ValueError, AssertionError, RuntimeError) as exc:
        raise DocumentParseError(f"Document read failed: {exc}") from exc
    finally:
        if spawned is not None and (started or spawned.pid is not None):
            child_reaped = kill_process(spawned)
        if tmp is not None:
            if child_reaped:
                with contextlib.suppress(OSError):
                    tmp.cleanup()
            elif spawned is not None:
                _QUARANTINED_JOBS.append((spawned, tmp))
        if not child_reaped:
            raise DocumentParseTimeoutError(
                "Document parser cleanup failed; worker capacity quarantined.", child_reaped=False
            )
