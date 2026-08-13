from typing import TypedDict, Unpack

from app.services.storage_runtime.s3 import S3StorageBackend


class BotocoreConfigOptions(TypedDict):
    max_pool_connections: int
    proxies: dict[str, str]
    s3: dict[str, str]
    signature_version: str
    connect_timeout: int
    read_timeout: int
    tcp_keepalive: bool
    region_name: str | None


def test_s3_backend_passes_max_pool_connections(monkeypatch):
    config_instances: list[FakeConfig] = []
    client_calls: list[FakeConfig] = []

    class FakeConfig:
        def __init__(self, **kwargs: Unpack[BotocoreConfigOptions]) -> None:
            self.kwargs = kwargs
            config_instances.append(self)

    class FakeS3Client:
        pass

    class FakeBoto3:
        def client(
            self,
            service_name: str,
            *,
            endpoint_url: str | None,
            aws_access_key_id: str | None,
            aws_secret_access_key: str | None,
            config: FakeConfig,
        ) -> FakeS3Client:
            assert service_name == "s3"
            assert endpoint_url == "http://minio:9000"
            assert aws_access_key_id == "key"
            assert aws_secret_access_key == "secret"
            client_calls.append(config)
            return FakeS3Client()

    class FakeBotocoreConfigModule:
        Config = FakeConfig

    fake_boto3 = FakeBoto3()

    import builtins

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "boto3":
            return fake_boto3
        if name == "botocore.config":
            return FakeBotocoreConfigModule()
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    backend = S3StorageBackend(
        bucket="bucket",
        endpoint_url="http://minio:9000",
        access_key_id="key",
        secret_access_key="secret",
        max_pool_connections=64,
    )

    backend._client_or_raise()

    assert len(config_instances) == 1
    assert config_instances[0].kwargs["max_pool_connections"] == 64
    assert len(client_calls) == 1
    assert client_calls[0] is config_instances[0]
