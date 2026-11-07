"""S3-compatible object storage backend."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping
from functools import partial
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import override

import anyio
from botocore.exceptions import ClientError

from app.core.json_types import json_as_int
from app.services.storage_runtime.base import (
    StorageDeleteFailedError,
    StorageEntry,
    StoragePage,
    StorageReadLimitExceededError,
    StorageVersion,
    checked_page_limit,
    walk_order,
)
from app.services.storage_runtime.s3_connection import S3ListArguments, S3ObjectIdentity
from app.services.storage_runtime.s3_create import S3CreateBackend
from app.services.storage_runtime.s3_read import read_object
from app.services.storage_runtime.utils import normalize_storage_key

_LAST_CODE_POINT = "\U0010ffff"


def _mapping_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in list[object](value) if isinstance(item, dict)]


class S3StorageBackend(S3CreateBackend):
    @override
    async def exists(self, key: str) -> bool:
        return await self._object_exists(key)

    @override
    async def is_file(self, key: str) -> bool:
        return await self._object_exists(key)

    async def _object_exists(self, key: str) -> bool:
        object_key = self._object_key(key)
        client = self._client_or_raise()
        response = await asyncio.to_thread(
            client.list_objects_v2,
            Bucket=self.bucket,
            Prefix=object_key,
            MaxKeys=1,
        )
        return any(item.get("Key") == object_key for item in _mapping_items(response.get("Contents")))

    @override
    async def is_dir(self, key: str) -> bool:
        prefix = self._object_key(key).rstrip("/") + "/"
        client = self._client_or_raise()
        response = await asyncio.to_thread(
            client.list_objects_v2,
            Bucket=self.bucket,
            Prefix=prefix,
            Delimiter="/",
            MaxKeys=1,
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
        for item in _mapping_items(page.get("CommonPrefixes")):
            rel = _strip_prefix(str(item.get("Prefix", "")).rstrip("/"), self.prefix)
            entries.append(StorageEntry(name=rel.split("/")[-1], key=rel, is_dir=True))
        for item in _mapping_items(page.get("Contents")):
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
            for item in _mapping_items(page.get("Contents")):
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

    async def _list_page(self, arguments: S3ListArguments) -> Mapping[str, object]:
        client = self._client_or_raise()
        return await anyio.to_thread.run_sync(partial(client.list_objects_v2, **arguments), abandon_on_cancel=True)

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
        client = self._client_or_raise()
        get_object = partial(client.get_object, Bucket=self.bucket, Key=self._object_key(key))
        return await anyio.to_thread.run_sync(read_object, get_object, None, abandon_on_cancel=True)

    @override
    async def read_bytes_bounded(self, key: str, max_bytes: int) -> bytes:
        client = self._client_or_raise()
        get_object = partial(client.get_object, Bucket=self.bucket, Key=self._object_key(key))
        return await anyio.to_thread.run_sync(read_object, get_object, max_bytes, abandon_on_cancel=True)

    @override
    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        # GCS S3-compatible API requires an explicit Content-Type; without it
        # the V4 signature body-hash is calculated on an empty content-type,
        # but GCS applies a different default - causing SignatureDoesNotMatch.
        resolved_ct = content_type or "application/octet-stream"
        client = self._client_or_raise()
        await anyio.to_thread.run_sync(
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
        await anyio.to_thread.run_sync(partial(client.delete_object, Bucket=self.bucket, Key=self._object_key(key)))

    @override
    async def delete_tree(self, key: str) -> None:
        client = self._client_or_raise()
        prefix = self._object_key(key).rstrip("/") + "/"
        failed = 0
        async for page in self._list_pages({"Bucket": self.bucket, "Prefix": prefix}):
            objects: list[S3ObjectIdentity] = []
            for item in _mapping_items(page.get("Contents")):
                item_key = item.get("Key")
                if isinstance(item_key, str):
                    objects.append({"Key": item_key})
            if not objects:
                continue
            response = await anyio.to_thread.run_sync(
                partial(client.delete_objects, Bucket=self.bucket, Delete={"Objects": objects})
            )
            failed += len(_mapping_items(response.get("Errors")))
        if failed:
            raise StorageDeleteFailedError(key, failed)

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
            response = await asyncio.to_thread(
                client.head_object,
                Bucket=self.bucket,
                Key=object_key,
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return StorageVersion(key=normalize_storage_key(key), exists=False, is_dir=False)
            raise
        return StorageVersion(
            key=normalize_storage_key(key),
            exists=True,
            is_dir=False,
            size=json_as_int(response.get("ContentLength")),
            modified_at=str(response.get("LastModified") or ""),
            etag=_clean_etag(response.get("ETag")),
            version_id=str(response.get("VersionId") or ""),
            content_hash=_clean_etag(response.get("ETag")),
        )

    async def _put_succeeded(self, key: str, expected_size: int) -> bool:
        try:
            entry = await self.stat(key)
        except Exception:
            return False
        return entry.size == expected_size

    @override
    async def local_path_for(self, key: str) -> Path | None:
        suffix = Path(normalize_storage_key(key)).suffix
        with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            path = Path(tmp.name)
        await self.write_local_copy(key, path)
        return path

    async def write_local_copy(self, key: str, path: Path) -> None:
        data = await self.read_bytes(key)
        _ = await asyncio.to_thread(path.write_bytes, data)

    @override
    async def presign_download_url(self, key: str, filename: str | None = None, inline: bool = False) -> str | None:
        client = self._client_or_raise()
        params: dict[str, str] = {"Bucket": self.bucket, "Key": self._object_key(key)}
        if filename not in (None, ""):
            disposition = "inline" if inline else "attachment"
            params["ResponseContentDisposition"] = f'{disposition}; filename="{filename}"'
        url = await asyncio.to_thread(
            client.generate_presigned_url,
            ClientMethod="get_object",
            Params=params,
            ExpiresIn=self.presign_ttl_seconds,
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
