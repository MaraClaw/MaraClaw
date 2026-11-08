from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import override

import anyio
from botocore.exceptions import ClientError, ParamValidationError

from app.services.storage_runtime.base import (
    AtomicCreateUnsupportedError,
    ConditionalWriteResult,
    StorageConditionalRaceError,
    StorageVersion,
)
from app.services.storage_runtime.s3_connection import S3Connection
from app.services.storage_runtime.upload_source import UploadSource, open_upload_source
from app.services.storage_runtime.utils import normalize_storage_key

_ATTEMPTS = 3


def _created(key: str, size: int, response: Mapping[str, object]) -> ConditionalWriteResult:
    return ConditionalWriteResult(
        ok=True,
        current_version=StorageVersion(
            key=normalize_storage_key(key),
            exists=True,
            is_dir=False,
            size=size,
            etag=str(response.get("ETag") or "").strip('"'),
            version_id=str(response.get("VersionId") or ""),
        ),
    )


def _create_failure(exc: ClientError, key: str, attempt: int) -> ConditionalWriteResult | None:
    """A 412 is an occupied key and ``None`` retries a 409; every other failure propagates."""
    code = exc.response.get("Error", {}).get("Code", "")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    match status, code:
        case (412, _) | (_, "PreconditionFailed" | "412"):
            return ConditionalWriteResult(ok=False, conflict=True)
        case (409, _) | (_, "ConditionalRequestConflict"):
            if attempt == _ATTEMPTS - 1:
                raise StorageConditionalRaceError(key, _ATTEMPTS) from exc
            return None
        case (_, "NotImplemented" | "UnsupportedOperation" | "InvalidRequest" | "InvalidArgument"):
            raise AtomicCreateUnsupportedError("S3 endpoint", key) from exc
        case _:
            raise exc


def _put_source(
    send: Callable[[UploadSource], Mapping[str, object]], key: str, source: UploadSource
) -> ConditionalWriteResult:
    """Send ``source`` from the worker that owns it, rewinding for each 409 retry, and close it on every exit."""
    with source:
        for attempt in range(_ATTEMPTS):
            try:
                response = send(source)
            except ParamValidationError as exc:
                raise AtomicCreateUnsupportedError("S3 SDK model", key) from exc
            except ClientError as exc:
                result = _create_failure(exc, key, attempt)
                if result is None:
                    continue
                return result
            return _created(key, source.size, response)
    raise StorageConditionalRaceError(key, _ATTEMPTS)


class S3CreateBackend(S3Connection):
    @override
    async def create_bytes_if_absent(
        self, key: str, data: bytes, content_type: str | None = None
    ) -> ConditionalWriteResult:
        if self._is_gcs():
            raise AtomicCreateUnsupportedError("GCS S3-compatible endpoint", key)
        client = self._client_or_raise()
        for attempt in range(_ATTEMPTS):
            try:
                response = await anyio.to_thread.run_sync(
                    partial(
                        client.put_object,
                        Bucket=self.bucket,
                        Key=self._object_key(key),
                        Body=data,
                        ContentType=content_type or "application/octet-stream",
                        IfNoneMatch="*",
                    )
                )
            except ParamValidationError as exc:
                raise AtomicCreateUnsupportedError("S3 SDK model", key) from exc
            except ClientError as exc:
                result = _create_failure(exc, key, attempt)
                if result is None:
                    continue
                return result
            return _created(key, len(data), response)
        raise StorageConditionalRaceError(key, _ATTEMPTS)

    @override
    async def create_file_if_absent(
        self, key: str, path: Path, content_type: str | None = None
    ) -> ConditionalWriteResult:
        """Stream ``path`` as one PutObject; the worker opens, sends and closes the file."""
        if self._is_gcs():
            raise AtomicCreateUnsupportedError("GCS S3-compatible endpoint", key)
        send = self._sender(key, content_type)
        return await anyio.to_thread.run_sync(lambda: _put_source(send, key, open_upload_source(path, with_md5=False)))

    async def create_from_source(
        self, key: str, source: UploadSource, content_type: str | None
    ) -> ConditionalWriteResult:
        """Stream an already-open source; the worker closes it. Callers hold it only until this returns."""
        if self._is_gcs():
            raise AtomicCreateUnsupportedError("GCS S3-compatible endpoint", key)
        send = self._sender(key, content_type)
        return await anyio.to_thread.run_sync(partial(_put_source, send, key, source))

    def _sender(self, key: str, content_type: str | None) -> Callable[[UploadSource], Mapping[str, object]]:
        client = self._client_or_raise()
        object_key = self._object_key(key)

        def send(source: UploadSource) -> Mapping[str, object]:
            return client.put_object(
                Bucket=self.bucket,
                Key=object_key,
                Body=source.body(),
                ContentLength=source.size,
                ContentType=content_type or "application/octet-stream",
                IfNoneMatch="*",
            )

        return send
