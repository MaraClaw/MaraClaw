"""Blocking S3 response ownership, including cancellation of the awaiter."""

import os
import re
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from tempfile import mkstemp
from threading import Lock
from typing import Protocol, TypeIs

import anyio
from botocore.exceptions import ClientError

from app.core.json_types import json_as_int
from app.services.storage_runtime.base import (
    MaterializedFile,
    StorageReadLimitExceededError,
    StorageSnapshot,
    StorageVersion,
    content_hash_bytes,
)
from app.services.storage_runtime.bounded import read_bounded

_COPY_CHUNK_BYTES = 64 * 1024


class _ResponseBody(Protocol):
    def read(self, amt: int | None = None) -> bytes: ...

    def close(self) -> None: ...


def _is_response_body(value: object) -> TypeIs[_ResponseBody]:
    return callable(getattr(value, "read", None)) and callable(getattr(value, "close", None))


def _get_response(get_object: Callable[[], Mapping[str, object]]) -> Mapping[str, object]:
    try:
        return get_object()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            raise FileNotFoundError from exc
        raise


def _response_body(response: Mapping[str, object]) -> _ResponseBody:
    body = response["Body"]
    if not _is_response_body(body):
        raise TypeError("S3 get_object response is missing a readable, closable Body")
    return body


def _open_body(get_object: Callable[[], Mapping[str, object]], range_offset: int | None = None) -> _ResponseBody:
    response = _get_response(get_object)
    body = _response_body(response)
    if range_offset is not None:
        content_range = str(response.get("ContentRange", ""))
        parsed = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
        if parsed is None or int(parsed[1]) != range_offset:
            body.close()
            raise OSError("S3 provider did not honor the requested byte range")
    return body


def read_object(
    get_object: Callable[[], Mapping[str, object]], max_bytes: int | None, range_offset: int | None = None
) -> bytes:
    """Own GET, read and close in one worker, even after the awaiter abandons it.

    Cancellation cannot interrupt a blocking SDK call. The worker completes the
    bounded read (subject to the client's read timeout) and always closes its body.
    """
    try:
        body = _open_body(get_object, range_offset)
    except ClientError as exc:
        if range_offset is not None and exc.response.get("Error", {}).get("Code") in {
            "416",
            "InvalidRange",
            "RequestedRangeNotSatisfiable",
        }:
            return b""
        raise
    with closing(body):
        if max_bytes is None:
            return body.read()
        return (
            read_bounded(body, max_bytes, range_read=True)
            if range_offset is not None
            else read_bounded(body, max_bytes)
        )


def read_object_snapshot(get_object: Callable[[], Mapping[str, object]], key: str, max_bytes: int) -> StorageSnapshot:
    """Own one GET, bounded content, identity and close even when abandoned."""
    response = _get_response(get_object)
    with closing(_response_body(response)) as body:
        declared_size = json_as_int(response.get("ContentLength"))
        if declared_size > max_bytes:
            raise StorageReadLimitExceededError(max_bytes, declared_size)
        data = read_bounded(body, max_bytes)
        etag = str(response.get("ETag") or "").strip('"')
        return StorageSnapshot(
            data=data,
            version=StorageVersion(
                key=key,
                exists=True,
                is_dir=False,
                size=len(data),
                modified_at=str(response.get("LastModified") or ""),
                etag=etag,
                version_id=str(response.get("VersionId") or ""),
                content_hash=etag,
            ),
            sha256=content_hash_bytes(data),
        )


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
