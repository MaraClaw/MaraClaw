"""S3-compatible object storage backend."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol, TypedDict, TypeIs, override

from app.core.json_types import json_as_int
from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    StorageBackend,
    StorageEntry,
    StorageVersion,
    WriteCondition,
)
from app.services.storage_runtime.utils import normalize_storage_key


class _S3ObjectIdentity(TypedDict):
    Key: str


class _ReadableBody(Protocol):
    def read(self) -> bytes: ...


class _S3Client(Protocol):
    def list_objects_v2(
        self,
        *,
        Bucket: str,
        Prefix: str = ...,
        Delimiter: str = ...,
        MaxKeys: int = ...,
    ) -> Mapping[str, object]: ...

    def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, object]: ...

    def head_object(self, *, Bucket: str, Key: str) -> Mapping[str, object]: ...

    def generate_presigned_url(
        self,
        ClientMethod: str,
        Params: Mapping[str, str],
        ExpiresIn: int,
    ) -> str: ...


class _AsyncS3Client(Protocol):
    async def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
    ) -> object: ...

    async def delete_object(self, *, Bucket: str, Key: str) -> object: ...

    async def delete_objects(
        self,
        *,
        Bucket: str,
        Delete: Mapping[str, list[_S3ObjectIdentity]],
    ) -> object: ...


class _AsyncS3ClientCM(Protocol):
    async def __aenter__(self) -> _AsyncS3Client: ...

    async def __aexit__(self, *args: object) -> bool | None: ...


class _AioBoto3Session(Protocol):
    def client(self, service_name: str, **kwargs: object) -> _AsyncS3ClientCM: ...


def _is_s3_client(value: object) -> TypeIs[_S3Client]:
    return value is not None


def _is_aioboto3_session(value: object) -> TypeIs[_AioBoto3Session]:
    return value is not None


def _is_async_s3_cm(value: object) -> TypeIs[_AsyncS3ClientCM]:
    return value is not None


def _is_readable_body(value: object) -> TypeIs[_ReadableBody]:
    return callable(getattr(value, "read", None))


def _mapping_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in list[object](value) if isinstance(item, dict)]


class S3StorageBackend(StorageBackend):
    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        region: str = "",
        endpoint_url: str = "",
        access_key_id: str = "",
        secret_access_key: str = "",
        presign_ttl_seconds: int = 3600,
        max_pool_connections: int = 50,
        write_workers: int = 32,
    ):
        self.bucket: str = bucket
        self.prefix: str = normalize_storage_key(prefix)
        self.region: str = region
        self.endpoint_url: str | None = endpoint_url or None
        self.access_key_id: str | None = access_key_id or None
        self.secret_access_key: str | None = secret_access_key or None
        self.presign_ttl_seconds: int = presign_ttl_seconds
        self.max_pool_connections: int = max_pool_connections
        self._client: _S3Client | None = None
        self._aioboto3_session: _AioBoto3Session | None = None

    def _object_key(self, key: str) -> str:
        normalized = normalize_storage_key(key)
        return f"{self.prefix}/{normalized}" if self.prefix else normalized

    def _is_gcs(self) -> bool:
        """Return True if the endpoint targets Google Cloud Storage."""
        if not self.endpoint_url:
            return False
        return "storage.googleapis.com" in self.endpoint_url

    def _boto_config(self) -> object:
        """Build a botocore Config appropriate for the target endpoint."""
        from botocore.config import Config

        if self._is_gcs():
            # GCS S3-compatible API requires virtual-hosted-style addressing
            # and an explicit region of "auto" for V4 signatures to verify.
            addressing = "virtual"
            region = "auto"
        else:
            addressing = "path"
            region = self.region or None
        return Config(
            max_pool_connections=self.max_pool_connections,
            proxies={},
            s3={"addressing_style": addressing},
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=30,
            tcp_keepalive=True,
            region_name=region,
        )

    def _client_or_raise(self) -> _S3Client:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:
                raise RuntimeError("boto3 is required for S3 storage backend") from exc
            client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                config=self._boto_config(),
            )
            if not _is_s3_client(client):
                raise RuntimeError("boto3 S3 client is missing required methods")
            self._client = client
        return self._client

    @asynccontextmanager
    async def _async_client(self) -> AsyncIterator[_AsyncS3Client]:
        """Shared aioboto3 session with aiohttp connection pool - reuses connections but detects stale ones correctly."""
        try:
            import aioboto3
        except ImportError as exc:
            raise RuntimeError("aioboto3 is required for async S3 writes") from exc
        session = self._aioboto3_session
        if session is None:
            raw_session = aioboto3.Session()
            if not _is_aioboto3_session(raw_session):
                raise RuntimeError("aioboto3 session is missing client()")
            session = raw_session
            self._aioboto3_session = session
        client_cm_raw = session.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            config=self._boto_config(),
        )
        if not _is_async_s3_cm(client_cm_raw):
            raise RuntimeError("aioboto3 S3 client context manager is missing")
        async with client_cm_raw as client:
            yield client

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
        prefix = self._object_key(key).rstrip("/")
        if prefix:
            prefix += "/"
        client = self._client_or_raise()
        response = await asyncio.to_thread(
            client.list_objects_v2,
            Bucket=self.bucket,
            Prefix=prefix,
            Delimiter="/",
        )
        entries: list[StorageEntry] = []
        for item in _mapping_items(response.get("CommonPrefixes")):
            raw = str(item.get("Prefix", "")).rstrip("/")
            rel = _strip_prefix(raw, self.prefix)
            name = rel.split("/")[-1]
            entries.append(StorageEntry(name=name, key=rel, is_dir=True))
        for item in _mapping_items(response.get("Contents")):
            raw = str(item.get("Key", ""))
            if not raw or raw == prefix:
                continue
            rel = _strip_prefix(raw, self.prefix)
            name = rel.split("/")[-1]
            entries.append(
                StorageEntry(
                    name=name,
                    key=rel,
                    is_dir=False,
                    size=json_as_int(item.get("Size")),
                    modified_at=str(item.get("LastModified") or ""),
                    etag=_clean_etag(item.get("ETag")),
                )
            )
        return sorted(entries, key=lambda entry: (not entry.is_dir, entry.name))

    @override
    async def read_bytes(self, key: str) -> bytes:
        client = self._client_or_raise()
        response = await asyncio.to_thread(
            client.get_object,
            Bucket=self.bucket,
            Key=self._object_key(key),
        )
        body = response["Body"]
        if not _is_readable_body(body):
            raise TypeError("S3 get_object response is missing a readable Body")
        return await asyncio.to_thread(body.read)

    @override
    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        # GCS S3-compatible API requires an explicit Content-Type; without it
        # the V4 signature body-hash is calculated on an empty content-type,
        # but GCS applies a different default - causing SignatureDoesNotMatch.
        resolved_ct = content_type or "application/octet-stream"
        async with self._async_client() as client:
            await client.put_object(
                Bucket=self.bucket,
                Key=self._object_key(key),
                Body=data,
                ContentType=resolved_ct,
            )

    @override
    async def delete(self, key: str) -> None:
        async with self._async_client() as client:
            await client.delete_object(
                Bucket=self.bucket,
                Key=self._object_key(key),
            )

    @override
    async def delete_tree(self, key: str) -> None:
        client = self._client_or_raise()
        prefix = self._object_key(key).rstrip("/") + "/"
        response = await asyncio.to_thread(
            client.list_objects_v2,
            Bucket=self.bucket,
            Prefix=prefix,
        )
        contents = _mapping_items(response.get("Contents"))
        if not contents:
            return
        objects: list[_S3ObjectIdentity] = []
        for item in contents:
            item_key = item.get("Key")
            if isinstance(item_key, str):
                objects.append({"Key": item_key})
        if not objects:
            return
        async with self._async_client() as client:
            await client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": objects},
            )

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
    async def get_version(self, key: str) -> StorageVersion:
        client = self._client_or_raise()
        object_key = self._object_key(key)
        try:
            response = await asyncio.to_thread(
                client.head_object,
                Bucket=self.bucket,
                Key=object_key,
            )
        except Exception:
            return StorageVersion(key=normalize_storage_key(key), exists=False, is_dir=False)
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

    @override
    async def write_bytes_if_match(
        self,
        key: str,
        data: bytes,
        *,
        condition: WriteCondition | None = None,
        content_type: str | None = None,
    ) -> ConditionalWriteResult:
        current = await self.get_version(key)
        if condition:
            if condition.require_absent and current.exists:
                return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
            if condition.version_token is not None and current.token != condition.version_token:
                return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
        await self.write_bytes(key, data, content_type=content_type)
        return ConditionalWriteResult(ok=True, current_version=await self.get_version(key))

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
        if filename:
            disposition = "inline" if inline else "attachment"
            params["ResponseContentDisposition"] = f'{disposition}; filename="{filename}"'
        url = await asyncio.to_thread(
            client.generate_presigned_url,
            "get_object",
            Params=params,
            ExpiresIn=self.presign_ttl_seconds,
        )
        if url and self.endpoint_url:
            from urllib.parse import urlparse, urlunparse

            parsed_url = urlparse(url)
            parsed_endpoint = urlparse(self.endpoint_url)
            if parsed_url.netloc == parsed_endpoint.netloc:
                # MinIO-style endpoint: rewrite path with /minio prefix
                new_path = "/minio" + parsed_url.path
                url = urlunparse(("", "", new_path, parsed_url.params, parsed_url.query, parsed_url.fragment))
            # GCS (storage.googleapis.com): presigned URLs are already correct, no rewrite needed
        return url


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
