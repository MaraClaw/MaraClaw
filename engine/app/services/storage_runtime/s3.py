"""S3-compatible object storage backend."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from functools import partial
from itertools import batched
from pathlib import Path
from typing import override

from botocore.exceptions import ClientError

from app.core.json_types import json_as_int
from app.services.storage_runtime.base import (
    BATCH_KEY_LIMIT,
    ConditionalWriteResult,
    CopyRequest,
    MaterializedFile,
    StorageDeleteFailedError,
    StorageEntry,
    StoragePage,
    StorageReadLimitExceededError,
    StorageSnapshot,
    StorageVersion,
    checked_page_limit,
    deleted_result,
    kept_result,
    walk_order,
)
from app.services.storage_runtime.disposition import content_disposition
from app.services.storage_runtime.s3_batch import copied_result, copy_conflict, delete_verdicts, quoted_etag
from app.services.storage_runtime.s3_connection import (
    S3CopySource,
    S3GetArguments,
    S3ListArguments,
    S3ObjectIdentity,
    response_version_id,
)
from app.services.storage_runtime.s3_create import S3CreateBackend
from app.services.storage_runtime.s3_read import materialize_object, read_object, read_object_snapshot
from app.services.storage_runtime.s3_types import client_error_response, mapping_items
from app.services.storage_runtime.utils import normalize_storage_key

_LAST_CODE_POINT = "\U0010ffff"


class S3StorageBackend(S3CreateBackend):
    @override
    async def exists(self, key: str) -> bool:
        return await self._object_exists(key)

    @override
    async def is_file(self, key: str) -> bool:
        return await self._object_exists(key)

    async def _object_exists(self, key: str) -> bool:
        object_key = self._object_key(key)
        client = self._read_client_or_raise()
        response = await self.lane.run(
            partial(client.list_objects_v2, Bucket=self.bucket, Prefix=object_key, MaxKeys=1)
        )
        return any(item.get("Key") == object_key for item in mapping_items(response.get("Contents")))

    @override
    async def is_dir(self, key: str) -> bool:
        prefix = self._object_key(key).rstrip("/") + "/"
        client = self._read_client_or_raise()
        response = await self.lane.run(
            partial(client.list_objects_v2, Bucket=self.bucket, Prefix=prefix, Delimiter="/", MaxKeys=1)
        )
        return bool(response.get("Contents") or response.get("CommonPrefixes"))

    @override
    async def list_dir(self, key: str) -> list[StorageEntry]:
        prefix = self._listing_prefix(key)
        entries: list[StorageEntry] = []
        async for page in self._list_pages({"Bucket": self.bucket, "Prefix": prefix, "Delimiter": "/"}):
            entries.extend(self._listing_entries(page, prefix))
        return sorted(entries, key=lambda entry: (not entry.is_dir, entry.name))

    @override
    async def list_dir_page(self, key: str, *, limit: int, after: str = "") -> StoragePage:
        bounded = checked_page_limit(limit)
        prefix = self._listing_prefix(key)
        first: S3ListArguments = {"Bucket": self.bucket, "Prefix": prefix, "Delimiter": "/"}
        arguments: S3ListArguments = {**first, "MaxKeys": bounded + 1}
        if after:
            arguments["StartAfter"] = _resume_after(prefix, after)
        entries: list[StorageEntry] = []
        truncated = False
        while True:
            page = await self._list_page(arguments)
            entries.extend(entry for entry in self._listing_entries(page, prefix) if walk_order(entry) > after)
            token = str(page.get("NextContinuationToken") or "")
            truncated = bool(page.get("IsTruncated"))
            if len(entries) > bounded or not truncated or not token:
                break
            arguments = {**first, "ContinuationToken": token, "MaxKeys": min(bounded + 1 - len(entries), 1000)}
        entries.sort(key=walk_order)
        return StoragePage(entries[:bounded], len(entries) > bounded or truncated)

    def _listing_prefix(self, key: str) -> str:
        prefix = self._object_key(key).rstrip("/")
        return f"{prefix}/" if prefix else ""

    def _listing_entries(self, page: Mapping[str, object], prefix: str) -> list[StorageEntry]:
        entries: list[StorageEntry] = []
        for item in mapping_items(page.get("CommonPrefixes")):
            rel = _strip_prefix(str(item.get("Prefix", "")).rstrip("/"), self.prefix)
            entries.append(StorageEntry(name=rel.split("/")[-1], key=rel, is_dir=True))
        for item in mapping_items(page.get("Contents")):
            raw = str(item.get("Key", ""))
            if raw and raw != prefix:
                entries.append(self._file_entry(item, _strip_prefix(raw, self.prefix)))
        return entries

    @override
    def walk(self, key: str) -> AsyncGenerator[StorageEntry]:
        return self._walk_flat(key)

    async def _walk_flat(self, key: str) -> AsyncGenerator[StorageEntry]:
        """One flat prefix listing in key order; directory entries are synthesized and hidden subtrees are jumped."""
        root = self._object_key(key).rstrip("/")
        prefix = root + "/" if root else ""
        first: S3ListArguments = {"Bucket": self.bucket, "Prefix": prefix}
        arguments: S3ListArguments = first
        open_dirs: list[str] = []
        skip_through = ""
        while True:
            page = await self._list_page(arguments)
            last_key = ""
            for item in mapping_items(page.get("Contents")):
                raw = str(item.get("Key", ""))
                last_key = max(last_key, raw)
                if not raw.startswith(prefix) or raw == prefix or raw <= skip_through:
                    continue
                parts = raw[len(prefix) :].split("/")
                is_marker = parts[-1] == ""
                names = parts[:-1] if is_marker else parts
                if "" in names:
                    continue
                hidden = next((index for index, name in enumerate(names) if name.startswith(".")), None)
                visible = names if hidden is None else names[:hidden]
                directories = visible if is_marker or hidden is not None else visible[:-1]
                shared = 0
                while shared < min(len(open_dirs), len(directories)) and open_dirs[shared] == directories[shared]:
                    shared += 1
                for depth in range(shared, len(directories)):
                    directory_key = _strip_prefix(f"{prefix}{'/'.join(directories[: depth + 1])}", self.prefix)
                    yield StorageEntry(name=directories[depth], key=directory_key, is_dir=True)
                open_dirs = directories
                if hidden is not None:
                    if is_marker or hidden < len(names) - 1:
                        skip_through = f"{prefix}{'/'.join(names[: hidden + 1])}0"
                    continue
                if not is_marker:
                    yield self._file_entry(item, _strip_prefix(raw, self.prefix))
            token = str(page.get("NextContinuationToken") or "")
            if not page.get("IsTruncated") or not token:
                return
            if skip_through and last_key <= skip_through:
                arguments = {**first, "StartAfter": skip_through}
            else:
                arguments = {**first, "ContinuationToken": token}

    @override
    async def list_files_flat(self, prefix: str) -> AsyncGenerator[StorageEntry]:
        """Every object under ``prefix`` from one delimiter-free listing; directory markers are not files."""
        object_prefix = self._listing_prefix(prefix)
        async for page in self._list_pages({"Bucket": self.bucket, "Prefix": object_prefix}):
            for item in mapping_items(page.get("Contents")):
                raw = str(item.get("Key", ""))
                if raw and raw != object_prefix and not raw.endswith("/"):
                    yield self._file_entry(item, _strip_prefix(raw, self.prefix))

    async def _list_page(self, arguments: S3ListArguments) -> Mapping[str, object]:
        client = self._read_client_or_raise()
        return await self.lane.run(partial(client.list_objects_v2, **arguments), abandon_on_cancel=True)

    async def _list_pages(self, arguments: S3ListArguments) -> AsyncGenerator[Mapping[str, object]]:
        page_arguments: S3ListArguments = arguments
        while True:
            page = await self._list_page(page_arguments)
            yield page
            token = str(page.get("NextContinuationToken") or "")
            if not page.get("IsTruncated") or not token:
                return
            page_arguments = {**arguments, "ContinuationToken": token}

    def _file_entry(self, item: Mapping[str, object], rel: str) -> StorageEntry:
        return StorageEntry(
            name=rel.split("/")[-1],
            key=rel,
            is_dir=False,
            size=json_as_int(item.get("Size")),
            modified_at=str(item.get("LastModified") or ""),
            etag=_clean_etag(item.get("ETag")),
        )

    @override
    async def read_bytes(self, key: str) -> bytes:
        client = self._read_client_or_raise()
        get_object = partial(client.get_object, Bucket=self.bucket, Key=self._object_key(key))
        return await self.lane.run(partial(read_object, get_object, None), abandon_on_cancel=True)

    @override
    async def read_bytes_bounded(self, key: str, max_bytes: int, *, offset: int | None = None) -> bytes:
        if max_bytes < 0 or (offset is not None and offset < 0):
            raise ValueError("max_bytes and offset must be nonnegative")
        client = self._read_client_or_raise()
        arguments: S3GetArguments = {"Bucket": self.bucket, "Key": self._object_key(key)}
        if offset is not None:
            if max_bytes == 0:
                return b""
            arguments["Range"] = f"bytes={offset}-{offset + max_bytes - 1}"
        get_object = partial(client.get_object, **arguments)
        return await self.lane.run(partial(read_object, get_object, max_bytes, offset), abandon_on_cancel=True)

    @override
    async def read_snapshot_bounded(self, key: str, max_bytes: int) -> StorageSnapshot:
        if max_bytes < 0:
            raise ValueError("max_bytes must be nonnegative")
        client = self._read_client_or_raise()
        get_object = partial(client.get_object, Bucket=self.bucket, Key=self._object_key(key))
        return await self.lane.run(
            partial(read_object_snapshot, get_object, normalize_storage_key(key), max_bytes), abandon_on_cancel=True
        )

    @override
    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        # GCS S3-compatible API requires an explicit Content-Type; without it
        # the V4 signature body-hash is calculated on an empty content-type,
        # but GCS applies a different default - causing SignatureDoesNotMatch.
        resolved_ct = content_type or "application/octet-stream"
        client = self._client_or_raise()
        await self.lane.run(
            partial(
                client.put_object,
                Bucket=self.bucket,
                Key=self._object_key(key),
                Body=data,
                ContentType=resolved_ct,
            )
        )

    @override
    async def delete(self, key: str) -> None:
        client = self._client_or_raise()
        await self.lane.run(partial(client.delete_object, Bucket=self.bucket, Key=self._object_key(key)))

    @override
    async def delete_tree(self, key: str) -> None:
        client = self._client_or_raise()
        prefix = self._object_key(key).rstrip("/") + "/"
        failed = 0
        async for page in self._list_pages({"Bucket": self.bucket, "Prefix": prefix}):
            objects: list[S3ObjectIdentity] = []
            for item in mapping_items(page.get("Contents")):
                item_key = item.get("Key")
                if isinstance(item_key, str):
                    objects.append({"Key": item_key})
            if not objects:
                continue
            response = await self.lane.run(
                partial(client.delete_objects, Bucket=self.bucket, Delete={"Objects": objects})
            )
            failed += len(mapping_items(response.get("Errors")))
        if failed:
            raise StorageDeleteFailedError(key, failed)

    @override
    async def delete_batch_if_match(self, versions: Sequence[StorageVersion]) -> dict[str, ConditionalWriteResult]:
        """One ``DeleteObjects`` per 1000 keys, each key carrying the ETag it was listed with.

        The provider compares that ETag as it deletes, so a key that changed since the listing comes back refused and
        stays. A version with no ETag matches no object and is left alone; keys the provider does not answer for raise.
        """
        client = self._client_or_raise()
        results: dict[str, ConditionalWriteResult] = {}
        unconfirmed: list[str] = []
        for chunk in batched(versions, BATCH_KEY_LIMIT, strict=False):
            sendable = {self._object_key(version.key): version for version in chunk if version.etag}
            results.update(
                {version.key: kept_result(version.key, exists=False) for version in chunk if not version.etag}
            )
            if not sendable:
                continue
            objects: list[S3ObjectIdentity] = [
                {"Key": object_key, "ETag": quoted_etag(version.etag)} for object_key, version in sendable.items()
            ]
            response = await self.lane.run(
                partial(client.delete_objects, Bucket=self.bucket, Delete={"Objects": objects})
            )
            verdicts = delete_verdicts(response, sendable)
            for object_key, version in sendable.items():
                if object_key in verdicts.deleted:
                    results[version.key] = deleted_result(version.key)
                elif object_key in verdicts.changed:
                    results[version.key] = kept_result(version.key, exists=True)
                elif object_key in verdicts.absent:
                    results[version.key] = kept_result(version.key, exists=False)
                else:
                    unconfirmed.append(version.key)
        if unconfirmed:
            raise StorageDeleteFailedError(unconfirmed[0], len(unconfirmed))
        return results

    @override
    async def copy_batch_if_match(self, copies: Sequence[CopyRequest]) -> dict[str, ConditionalWriteResult]:
        """A server-side ``CopyObject`` per file, sent only while the source still has its listed ETag; no body is read."""
        return {copy.source.key: await self.copy_if_match(copy) for copy in copies}

    async def copy_if_match(self, copy: CopyRequest) -> ConditionalWriteResult:
        """Copy one object on the provider; a source that changed or is gone is a conflict, every other failure raises."""
        if not copy.source.etag:
            return kept_result(copy.source.key, exists=False)
        client = self._client_or_raise()
        source: S3CopySource = {"Bucket": self.bucket, "Key": self._object_key(copy.source.key)}
        try:
            response = await self.lane.run(
                partial(
                    client.copy_object,
                    Bucket=self.bucket,
                    Key=self._object_key(copy.destination_key),
                    CopySource=source,
                    CopySourceIfMatch=quoted_etag(copy.source.etag),
                )
            )
        except ClientError as exc:
            conflict = copy_conflict(exc, copy.source.key)
            if conflict is None:
                raise
            return conflict
        return copied_result(copy.destination_key, copy.source.size, response)

    @override
    async def stat(self, key: str) -> StorageEntry:
        version = await self.get_version(key)
        if not version.exists:
            raise FileNotFoundError(key)
        return StorageEntry(
            name=normalize_storage_key(key).split("/")[-1],
            key=normalize_storage_key(key),
            is_dir=version.is_dir,
            size=version.size,
            modified_at=version.modified_at,
            etag=version.etag,
            version_id=version.version_id,
            content_hash=version.content_hash,
        )

    @override
    async def get_version_bounded(self, key: str, max_bytes: int) -> StorageVersion:
        version = await self.get_version(key)
        if version.exists and version.size > max_bytes:
            raise StorageReadLimitExceededError(max_bytes, version.size)
        return version

    @override
    async def get_version(self, key: str) -> StorageVersion:
        client = self._client_or_raise()
        object_key = self._object_key(key)
        try:
            response = await self.lane.run(partial(client.head_object, Bucket=self.bucket, Key=object_key))
        except ClientError as exc:
            if client_error_response(exc).get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return StorageVersion(key=normalize_storage_key(key), exists=False, is_dir=False)
            raise
        return StorageVersion(
            key=normalize_storage_key(key),
            exists=True,
            is_dir=False,
            size=json_as_int(response.get("ContentLength")),
            modified_at=str(response.get("LastModified") or ""),
            etag=_clean_etag(response.get("ETag")),
            version_id=response_version_id(response),
            content_hash=_clean_etag(response.get("ETag")),
        )

    async def _put_succeeded(self, key: str, expected_size: int) -> bool:
        try:
            entry = await self.stat(key)
        except Exception:
            return False
        return entry.size == expected_size

    @override
    async def materialize(self, key: str) -> MaterializedFile:
        client = self._read_client_or_raise()
        get_object = partial(client.get_object, Bucket=self.bucket, Key=self._object_key(key))
        return await materialize_object(self.lane, get_object, Path(normalize_storage_key(key)).suffix)

    @override
    async def presign_download_url(
        self, key: str, filename: str | None = None, inline: bool = False, expires_in_seconds: int | None = None
    ) -> str | None:
        client = self._client_or_raise()
        params: dict[str, str] = {"Bucket": self.bucket, "Key": self._object_key(key)}
        if filename is not None and filename != "":
            params["ResponseContentDisposition"] = content_disposition(filename, "inline" if inline else "attachment")
        url = await self.lane.run(
            partial(
                client.generate_presigned_url,
                ClientMethod="get_object",
                Params=params,
                ExpiresIn=self.presign_ttl_seconds if expires_in_seconds is None else expires_in_seconds,
            )
        )
        if url and self.endpoint_url not in (None, ""):
            from urllib.parse import urlparse, urlunparse

            parsed_url = urlparse(url)
            parsed_endpoint = urlparse(self.endpoint_url)
            if parsed_url.netloc == parsed_endpoint.netloc:
                # MinIO-style endpoint: rewrite path with /minio prefix
                new_path = "/minio" + parsed_url.path
                url = urlunparse(("", "", new_path, parsed_url.params, parsed_url.query, parsed_url.fragment))
            # GCS (storage.googleapis.com): presigned URLs are already correct, no rewrite needed
        return url


def _resume_after(prefix: str, after: str) -> str:
    """StartAfter key that skips what ``after`` consumed: a folder's whole subtree, or just the file."""
    return f"{prefix}{after}{_LAST_CODE_POINT}" if after.endswith("/") else f"{prefix}{after}"


def _strip_prefix(raw_key: str, prefix: str) -> str:
    if prefix and raw_key.startswith(prefix + "/"):
        return raw_key[len(prefix) + 1 :]
    return raw_key


def _is_header_parsing_error(exc: Exception) -> bool:
    try:
        from urllib3.exceptions import HeaderParsingError
    except Exception:
        return False
    return isinstance(exc, HeaderParsingError)


def _clean_etag(raw: object) -> str:
    if raw is None:
        return ""
    text = str(raw)
    return text.strip('"')
