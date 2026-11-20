"""Owned temp copies made by a worker thread, with exactly one remover even when the awaiter is cancelled."""

import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from tempfile import mkstemp
from threading import Lock

import anyio

from app.services.storage_runtime.base import MaterializedFile
from app.services.storage_runtime.bounded import BoundedReader

_COPY_CHUNK_BYTES = 64 * 1024

type SourceOpener = Callable[[], AbstractContextManager[BoundedReader]]


class _TempHandoff:
    """Gives a worker-made temp file exactly one remover: the worker once abandoned, else whoever received it."""

    def __init__(self) -> None:
        self._lock: Lock = Lock()
        self._published: Path | None = None
        self._abandoned: bool = False

    def publish(self, path: Path) -> bool:
        with self._lock:
            if self._abandoned:
                return False
            self._published = path
            return True

    def abandon(self) -> Path | None:
        with self._lock:
            self._abandoned = True
            return self._published


def _copy(open_source: SourceOpener, suffix: str, handoff: _TempHandoff) -> Path:
    """Own the source, the temp file, every chunk and close in one worker; remove the file unless the awaiter took it."""
    with open_source() as source:
        descriptor, name = mkstemp(suffix=suffix)
        path = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as target:
                while chunk := source.read(_COPY_CHUNK_BYTES):
                    _ = target.write(chunk)
            if not handoff.publish(path):
                path.unlink(missing_ok=True)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    return path


async def materialize_copy(open_source: SourceOpener, suffix: str) -> MaterializedFile:
    """Copy what `open_source` opens into a temp file made by the worker; the returned handle owns it."""
    handoff = _TempHandoff()
    try:
        path = await anyio.to_thread.run_sync(_copy, open_source, suffix, handoff, abandon_on_cancel=True)
    except BaseException:
        delivered = handoff.abandon()
        if delivered is not None:
            delivered.unlink(missing_ok=True)
        raise
    return MaterializedFile(path, owned=True)
