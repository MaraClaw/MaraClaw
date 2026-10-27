from functools import partial
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
from app.services.storage_runtime.utils import normalize_storage_key


class S3CreateBackend(S3Connection):
    @override
    async def create_bytes_if_absent(
        self, key: str, data: bytes, content_type: str | None = None
    ) -> ConditionalWriteResult:
        if self._is_gcs():
            raise AtomicCreateUnsupportedError("GCS S3-compatible endpoint", key)
        client = self._client_or_raise()
        for attempt in range(3):
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
                code = exc.response.get("Error", {}).get("Code", "")
                status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                match status, code:
                    case (412, _) | (_, "PreconditionFailed" | "412"):
                        return ConditionalWriteResult(ok=False, conflict=True)
                    case (409, _) | (_, "ConditionalRequestConflict"):
                        if attempt == 2:
                            raise StorageConditionalRaceError(key, 3) from exc
                        continue
                    case (_, "NotImplemented" | "UnsupportedOperation" | "InvalidRequest" | "InvalidArgument"):
                        raise AtomicCreateUnsupportedError("S3 endpoint", key) from exc
                    case _:
                        raise
            return ConditionalWriteResult(
                ok=True,
                current_version=StorageVersion(
                    key=normalize_storage_key(key),
                    exists=True,
                    is_dir=False,
                    size=len(data),
                    etag=str(response.get("ETag") or "").strip('"'),
                    version_id=str(response.get("VersionId") or ""),
                ),
            )
        raise StorageConditionalRaceError(key, 3)
