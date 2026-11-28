from collections.abc import Mapping
from typing import TYPE_CHECKING, Final, NotRequired, Protocol, TypedDict, TypeIs, Unpack

if TYPE_CHECKING:
    from botocore.config import Config

from app.core.thread_lanes import ThreadLane
from app.services.storage_runtime.base import StorageBackend
from app.services.storage_runtime.s3_types import new_s3_client
from app.services.storage_runtime.upload_source import SizedBody
from app.services.storage_runtime.utils import normalize_storage_key

# Standard mode backs a throttled call off by rand(0, 1) * 2**i seconds (at most 1, 2, 4), so four attempts ride out a
# few seconds of S3 throttling. Only the read client gets them: a retried write could land and then report a rejection.
READ_RETRIES: Final = {"total_max_attempts": 4, "mode": "standard"}
# Calls that may wait for each lane thread. A saturated lane refuses the next one at once instead of queueing it.
LANE_WAITERS_PER_THREAD: Final = 8


class S3ObjectIdentity(TypedDict):
    Key: str
    ETag: NotRequired[str]


class S3ObjectArguments(TypedDict):
    Bucket: str
    Key: str


class S3GetArguments(S3ObjectArguments):
    Range: NotRequired[str]


class S3ListArguments(TypedDict):
    Bucket: str
    Prefix: NotRequired[str]
    Delimiter: NotRequired[str]
    MaxKeys: NotRequired[int]
    ContinuationToken: NotRequired[str]
    StartAfter: NotRequired[str]


class S3PutArguments(S3ObjectArguments):
    Body: bytes | SizedBody
    ContentType: str
    ContentLength: NotRequired[int]
    IfNoneMatch: NotRequired[str]


class S3CopySource(TypedDict):
    Bucket: str
    Key: str


class S3CopyArguments(S3ObjectArguments):
    CopySource: S3CopySource
    CopySourceIfMatch: str


class S3PresignArguments(TypedDict):
    ClientMethod: str
    Params: Mapping[str, str]
    ExpiresIn: int


class S3DeleteArguments(TypedDict):
    Bucket: str
    Delete: Mapping[str, list[S3ObjectIdentity]]


class S3Client(Protocol):
    def list_objects_v2(self, **kwargs: Unpack[S3ListArguments]) -> Mapping[str, object]: ...

    def get_object(self, **kwargs: Unpack[S3GetArguments]) -> Mapping[str, object]: ...

    def head_object(self, **kwargs: Unpack[S3ObjectArguments]) -> Mapping[str, object]: ...

    def generate_presigned_url(self, **kwargs: Unpack[S3PresignArguments]) -> str: ...

    def put_object(self, **kwargs: Unpack[S3PutArguments]) -> Mapping[str, object]: ...

    def delete_object(self, **kwargs: Unpack[S3ObjectArguments]) -> object: ...

    def delete_objects(self, **kwargs: Unpack[S3DeleteArguments]) -> Mapping[str, object]: ...

    def copy_object(self, **kwargs: Unpack[S3CopyArguments]) -> Mapping[str, object]: ...


def _is_s3_client(value: object) -> TypeIs[S3Client]:
    return value is not None


def response_version_id(response: Mapping[str, object]) -> str:
    """The response's VersionId, or "" when there is none; S3 names an unversioned object's version "null"."""
    version_id = str(response.get("VersionId") or "")
    return "" if version_id == "null" else version_id


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
        thread_lane_size: int = 32,
    ):
        self.bucket: str = bucket
        self.prefix: str = normalize_storage_key(prefix)
        self.region: str = region
        self.endpoint_url: str | None = endpoint_url or None
        self.access_key_id: str | None = access_key_id or None
        self.secret_access_key: str | None = secret_access_key or None
        self.presign_ttl_seconds: int = presign_ttl_seconds
        self.max_pool_connections: int = max_pool_connections
        lane_size = min(thread_lane_size, max_pool_connections)
        self.lane: ThreadLane = ThreadLane(lane_size, name="s3-lane", max_waiters=lane_size * LANE_WAITERS_PER_THREAD)
        self._client: S3Client | None = None
        self._read_client: S3Client | None = None

    def _object_key(self, key: str) -> str:
        normalized = normalize_storage_key(key)
        return f"{self.prefix}/{normalized}" if self.prefix else normalized

    def _is_gcs(self) -> bool:
        if self.endpoint_url in (None, ""):
            return False
        return "storage.googleapis.com" in self.endpoint_url

    def _boto_config(self) -> Config:
        """The mutation client's config: the shared endpoint settings and the SDK's own retries."""
        return self._endpoint_config()

    def _read_boto_config(self) -> Config:
        """The read client's config: the same endpoint, credentials and pool as the mutation client, with retries."""
        return self._endpoint_config(READ_RETRIES)

    def _endpoint_config(self, retries: Mapping[str, int | str] | None = None) -> Config:
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
            retries=None if retries is None else dict(retries),
        )

    def _client_or_raise(self) -> S3Client:
        """The mutation client, for every put, copy, delete and `get_version`; a managed backend sends each exactly once."""
        if self._client is None:
            self._client = self._new_client(self._boto_config())
        return self._client

    def _read_client_or_raise(self) -> S3Client:
        """The read client, for calls that change nothing (listing, GET). Never use it for a write or `get_version`."""
        if self._read_client is None:
            self._read_client = self._new_client(self._read_boto_config())
        return self._read_client

    def _new_client(self, config: Config) -> S3Client:
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("boto3 is required for S3 storage backend") from exc
        client = new_s3_client(
            boto3.Session(),
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            config=config,
        )
        if not _is_s3_client(client):
            raise RuntimeError("boto3 S3 client is missing required methods")
        return client
