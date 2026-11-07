"""Blocking S3 response ownership, including cancellation of the awaiter."""

import os
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from tempfile import mkstemp
from threading import Lock
from typing import Protocol, TypeIs

import anyio
from botocore.exceptions import ClientError

from app.services.storage_runtime.base import MaterializedFile
from app.services.storage_runtime.bounded import read_bounded

_COPY_CHUNK_BYTES = 64 * 1024


class _ResponseBody(Protocol):
    def read(self, amt: int | None = None) -> bytes: ...

    def close(self) -> None: ...


def _is_response_body(value: object) -> TypeIs[_ResponseBody]:
    return callable(getattr(value, "read", None)) and callable(getattr(value, "close", None))


def _open_body(get_object: Callable[[], Mapping[str, object]]) -> _ResponseBody:
    try:
        response = get_object()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            raise FileNotFoundError from exc
        raise
    body = response["Body"]
    if not _is_response_body(body):
        raise TypeError("S3 get_object response is missing a readable, closable Body")
    return body


def read_object(get_object: Callable[[], Mapping[str, object]], max_bytes: int | None) -> bytes:
    """Own GET, read and close in one worker, even after the awaiter abandons it.

    Cancellation cannot interrupt a blocking SDK call. The worker completes the
    bounded read (subject to the client's read timeout) and always closes its body.
    """
    with closing(_open_body(get_object)) as body:
        if max_bytes is None:
            return body.read()
        return read_bounded(body, max_bytes)


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


def _copy_object(get_object: Callable[[], Mapping[str, object]], suffix: str, handoff: _TempHandoff) -> Path:
    """Own GET, the temp file, every chunk and close in one worker; remove the file unless the awaiter took it."""
    with closing(_open_body(get_object)) as body:
        descriptor, name = mkstemp(suffix=suffix)
        path = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as target:
                while chunk := body.read(_COPY_CHUNK_BYTES):
                    _ = target.write(chunk)
            if not handoff.publish(path):
                path.unlink(missing_ok=True)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    return path


async def materialize_object(get_object: Callable[[], Mapping[str, object]], suffix: str) -> MaterializedFile:
    """Stream the object into a temp file made by the worker; the returned handle owns it."""
    handoff = _TempHandoff()
    try:
        path = await anyio.to_thread.run_sync(_copy_object, get_object, suffix, handoff, abandon_on_cancel=True)
    except BaseException:
        delivered = handoff.abandon()
        if delivered is not None:
            delivered.unlink(missing_ok=True)
        raise
    return MaterializedFile(path, owned=True)
