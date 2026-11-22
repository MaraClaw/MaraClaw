"""Blocking S3 response ownership, including cancellation of the awaiter."""

import re
from collections.abc import Callable, Mapping
from contextlib import closing
from typing import Protocol, TypeIs

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
from app.services.storage_runtime.owned_copy import materialize_copy
from app.services.storage_runtime.s3_connection import response_version_id
from app.services.storage_runtime.s3_types import client_error_response


class _ResponseBody(Protocol):
    def read(self, amt: int | None = None) -> bytes: ...

    def close(self) -> None: ...


def _is_response_body(value: object) -> TypeIs[_ResponseBody]:
    return callable(getattr(value, "read", None)) and callable(getattr(value, "close", None))


def _get_response(get_object: Callable[[], Mapping[str, object]]) -> Mapping[str, object]:
    try:
        return get_object()
    except ClientError as exc:
        if client_error_response(exc).get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
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
        if range_offset is not None and client_error_response(exc).get("Error", {}).get("Code") in {
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
                version_id=response_version_id(response),
                content_hash=etag,
            ),
            sha256=content_hash_bytes(data),
        )


async def materialize_object(get_object: Callable[[], Mapping[str, object]], suffix: str) -> MaterializedFile:
    """Stream the object into a temp file made by the worker; the returned handle owns it."""
    return await materialize_copy(lambda: closing(_open_body(get_object)), suffix)
