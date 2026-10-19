from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import NotRequired, Protocol, TypedDict, TypeIs, Unpack

from app.services.storage_runtime.base import StorageBackend
from app.services.storage_runtime.utils import normalize_storage_key


class S3ObjectIdentity(TypedDict):
    Key: str


class S3ObjectArguments(TypedDict):
    Bucket: str
    Key: str


class S3ListArguments(TypedDict):
    Bucket: str
    Prefix: NotRequired[str]
    Delimiter: NotRequired[str]
    MaxKeys: NotRequired[int]


class S3PutArguments(S3ObjectArguments):
    Body: bytes
    ContentType: str
    IfNoneMatch: NotRequired[str]


class S3PresignArguments(TypedDict):
    ClientMethod: str
    Params: Mapping[str, str]
    ExpiresIn: int


class S3DeleteArguments(TypedDict):
    Bucket: str
    Delete: Mapping[str, list[S3ObjectIdentity]]


class S3Client(Protocol):
    def list_objects_v2(self, **kwargs: Unpack[S3ListArguments]) -> Mapping[str, object]: ...

    def get_object(self, **kwargs: Unpack[S3ObjectArguments]) -> Mapping[str, object]: ...

    def head_object(self, **kwargs: Unpack[S3ObjectArguments]) -> Mapping[str, object]: ...

    def generate_presigned_url(self, **kwargs: Unpack[S3PresignArguments]) -> str: ...


class AsyncS3Client(Protocol):
    async def put_object(self, **kwargs: Unpack[S3PutArguments]) -> Mapping[str, object]: ...

    async def delete_object(self, **kwargs: Unpack[S3ObjectArguments]) -> object: ...

    async def delete_objects(self, **kwargs: Unpack[S3DeleteArguments]) -> object: ...


class AsyncS3ClientCM(Protocol):
    async def __aenter__(self) -> AsyncS3Client: ...

    async def __aexit__(self, *args: object) -> bool | None: ...


class AioBoto3Session(Protocol):
    def client(self, service_name: str, **kwargs: object) -> AsyncS3ClientCM: ...


def _is_s3_client(value: object) -> TypeIs[S3Client]:
    return value is not None


def _is_aioboto3_session(value: object) -> TypeIs[AioBoto3Session]:
    return value is not None


def _is_async_s3_cm(value: object) -> TypeIs[AsyncS3ClientCM]:
    return value is not None


class S3Connection(StorageBackend):
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
        self._client: S3Client | None = None
        self._aioboto3_session: AioBoto3Session | None = None

    def _object_key(self, key: str) -> str:
        normalized = normalize_storage_key(key)
        return f"{self.prefix}/{normalized}" if self.prefix else normalized

    def _is_gcs(self) -> bool:
        if self.endpoint_url in (None, ""):
            return False
        return "storage.googleapis.com" in self.endpoint_url

    def _boto_config(self) -> object:
        from botocore.config import Config

        if self._is_gcs():
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

    def _client_or_raise(self) -> S3Client:
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
    async def _async_client(self) -> AsyncGenerator[AsyncS3Client]:
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
