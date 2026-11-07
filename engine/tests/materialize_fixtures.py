"""Shared fixtures for temp-copy tests: an isolated temp root and one S3 object that can pause mid-copy."""

import tempfile
from io import BytesIO
from pathlib import Path
from threading import Event, get_ident
from typing import Literal, override

import pytest

from app.services.storage_runtime.s3 import S3StorageBackend

type Pause = Literal["", "get", "read"]


@pytest.fixture
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty directory that every ``tempfile`` call in the test uses, so leaks are countable."""
    root = tmp_path / "temp-root"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def temp_files(root: Path) -> list[Path]:
    return sorted(root.iterdir())


def temp_contents(root: Path) -> list[bytes]:
    return [path.read_bytes() for path in temp_files(root)]


class GatedObject:
    """One S3 object behind HEAD, GET and a prefix listing; GET or the first body read can block until ``release``."""

    def __init__(self, key: str, data: bytes, *, pause: Pause = "", fail_after: int | None = None):
        self.key: str = key
        self.data: bytes = data
        self.pause: Pause = pause
        self.fail_after: int | None = fail_after
        self.reported_size: int | None = None
        self.blocked: Event = Event()
        self.release: Event = Event()
        self.closed: Event = Event()
        self.owners: list[int] = []
        self.read_sizes: list[int | None] = []
        self.get_calls: int = 0

    def wait_here(self) -> None:
        self.blocked.set()
        assert self.release.wait(10)

    def head_object(self, **request: str) -> dict[str, object]:
        assert request["Key"] == self.key
        return {"ContentLength": len(self.data) if self.reported_size is None else self.reported_size, "ETag": '"e"'}

    def list_objects_v2(self, **request: str) -> dict[str, object]:
        return {"Contents": [{"Key": self.key}] if self.key.startswith(request["Prefix"]) else []}

    def generate_presigned_url(self, **request: object) -> str:
        return ""

    def get_object(self, **request: str) -> dict[str, object]:
        assert request["Key"] == self.key
        self.get_calls += 1
        self.owners.append(get_ident())
        if self.pause == "get":
            self.wait_here()
        return {"Body": ObjectBody(self)}

    def backend(self, monkeypatch: pytest.MonkeyPatch) -> S3StorageBackend:
        storage = S3StorageBackend(bucket="bucket")
        monkeypatch.setattr(storage, "_client_or_raise", lambda: self)
        return storage


class ObjectBody(BytesIO):
    def __init__(self, source: GatedObject):
        super().__init__(source.data)
        self.source: GatedObject = source
        self.first_read: bool = True

    @override
    def read(self, size: int | None = -1) -> bytes:
        self.source.owners.append(get_ident())
        self.source.read_sizes.append(size)
        if self.first_read and self.source.pause == "read":
            self.source.wait_here()
        self.first_read = False
        if self.source.fail_after is not None and self.tell() >= self.source.fail_after:
            raise OSError("stream failed")
        return super().read(size)

    @override
    def close(self) -> None:
        self.source.owners.append(get_ident())
        super().close()
        self.source.closed.set()
