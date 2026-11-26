from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import override

from botocore.exceptions import ClientError, ParamValidationError

from app.services.storage_runtime.base import (
    AtomicCreateUnsupportedError,
    ConditionalWriteResult,
    StorageConditionalRaceError,
    StorageVersion,
    WriteCondition,
)
from app.services.storage_runtime.s3_connection import S3Connection, S3PutArguments, response_version_id
from app.services.storage_runtime.s3_types import client_error_response
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
            version_id=response_version_id(response),
        ),
    )


def _create_failure(exc: ClientError, key: str, attempt: int) -> ConditionalWriteResult | None:
    """A 412 is an occupied key and ``None`` retries a 409; every other failure propagates."""
    response = client_error_response(exc)
    code = response.get("Error", {}).get("Code", "")
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
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
    """Send ``source`` from the lane worker, rewinding for each 409 retry; the caller keeps ownership and closes it."""
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


def _put_file(send: Callable[[UploadSource], Mapping[str, object]], key: str, path: Path) -> ConditionalWriteResult:
    with open_upload_source(path, with_md5=False) as source:
        return _put_source(send, key, source)


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
                response = await self.lane.run(
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
        return await self.lane.run(partial(_put_file, self._sender(key, content_type, absent=True), key, path))

    async def create_from_source(
        self, key: str, source: UploadSource, content_type: str | None
    ) -> ConditionalWriteResult:
        """Stream an already-open source as one PutObject that fails on an occupied key; the caller closes it."""
        if self._is_gcs():
            raise AtomicCreateUnsupportedError("GCS S3-compatible endpoint", key)
        return await self.lane.run(partial(_put_source, self._sender(key, content_type, absent=True), key, source))

    async def put_source(self, key: str, source: UploadSource, content_type: str | None) -> ConditionalWriteResult:
        """Stream an already-open source as one unconditional PutObject; the caller closes it."""
        return await self.lane.run(partial(_put_source, self._sender(key, content_type, absent=False), key, source))

    @override
    async def write_source_if_match(
        self,
        key: str,
        source: UploadSource,
        *,
        condition: WriteCondition | None = None,
        content_type: str | None = None,
    ) -> ConditionalWriteResult:
        if condition is not None and condition.require_absent:
            return await self.create_from_source(key, source, content_type)
        current = await self.get_version(key)
        if condition is not None and condition.version_token is not None and current.token != condition.version_token:
            return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
        return await self.put_source(key, source, content_type)

    def _sender(
        self, key: str, content_type: str | None, *, absent: bool
    ) -> Callable[[UploadSource], Mapping[str, object]]:
        client = self._client_or_raise()
        object_key = self._object_key(key)

        def send(source: UploadSource) -> Mapping[str, object]:
            arguments: S3PutArguments = {
                "Bucket": self.bucket,
                "Key": object_key,
                "Body": source.body(),
                "ContentLength": source.size,
                "ContentType": content_type or "application/octet-stream",
            }
            if absent:
                arguments["IfNoneMatch"] = "*"
            return client.put_object(**arguments)

        return send
