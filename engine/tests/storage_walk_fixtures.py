"""Shared fixtures for workspace-walk tests: a paging in-memory S3 client and one tree built on S3 and on disk."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import override

import pytest

from app.services.storage_runtime.base import StorageBackend, StorageEntry
from app.services.storage_runtime.s3 import S3StorageBackend

FILE_SIZE = 3

# Awkward on purpose: byte order puts "a-b.txt" < "a.txt" < "a/" < "a0", and uppercase and non-ASCII names sort by bytes.
TREE_KEYS = (
    "w/a-b.txt",
    "w/a.txt",
    "w/a/inner.txt",
    "w/a0",
    "w/B.txt",
    "w/b/deep/leaf.md",
    "w/empty/",
    "w/\u00e9.txt",
    "w/.hidden/secret.txt",
    "w/.env",
    "w/b/.gitkeep",
)

EXPECTED_WALK = (
    ("B.txt", "w/B.txt", False),
    ("a-b.txt", "w/a-b.txt", False),
    ("a.txt", "w/a.txt", False),
    ("a", "w/a", True),
    ("inner.txt", "w/a/inner.txt", False),
    ("a0", "w/a0", False),
    ("b", "w/b", True),
    ("deep", "w/b/deep", True),
    ("leaf.md", "w/b/deep/leaf.md", False),
    ("empty", "w/empty", True),
    ("\u00e9.txt", "w/\u00e9.txt", False),
)


@dataclass
class PagedS3Client:
    """Just enough ListObjectsV2/DeleteObjects: byte-ordered keys, delimiters, pages, StartAfter and tokens."""

    keys: dict[str, int] = field(default_factory=dict)
    page_size: int = 1000
    refuse_deletes: frozenset[str] = frozenset()
    list_calls: list[dict[str, str | int]] = field(default_factory=list)
    delete_calls: list[list[str]] = field(default_factory=list)

    def list_objects_v2(self, **kwargs: str | int) -> Mapping[str, object]:
        self.list_calls.append(dict(kwargs))
        prefix, delimiter = str(kwargs.get("Prefix", "")), str(kwargs.get("Delimiter", ""))
        after = str(kwargs.get("ContinuationToken") or kwargs.get("StartAfter") or "")
        page_size = min(self.page_size, int(kwargs.get("MaxKeys", self.page_size)))
        rows: list[tuple[str, str, bool]] = []
        for key in sorted(self.keys):
            if not key.startswith(prefix) or key <= after:
                continue
            rest = key.removeprefix(prefix)
            if delimiter and delimiter in rest:
                common = prefix + rest.split(delimiter, 1)[0] + delimiter
                if rows and rows[-1][0] == common and rows[-1][2]:
                    rows[-1] = (common, key, True)
                else:
                    rows.append((common, key, True))
            else:
                rows.append((key, key, False))
        page = rows[:page_size]
        response: dict[str, object] = {
            "Contents": [
                {"Key": key, "Size": self.keys[key], "ETag": '"etag"', "LastModified": "then"}
                for key, _last, is_prefix in page
                if not is_prefix
            ],
            "CommonPrefixes": [{"Prefix": key} for key, _last, is_prefix in page if is_prefix],
            "IsTruncated": len(rows) > len(page),
        }
        if len(rows) > len(page):
            response["NextContinuationToken"] = page[-1][1]
        return response

    def delete_objects(self, **kwargs: Mapping[str, list[Mapping[str, str]]]) -> Mapping[str, object]:
        requested = [item["Key"] for item in kwargs["Delete"]["Objects"]]
        self.delete_calls.append(requested)
        refused = [key for key in requested if key in self.refuse_deletes]
        for key in requested:
            if key not in refused:
                _ = self.keys.pop(key, None)
        return {
            "Deleted": [{"Key": key} for key in requested if key not in refused],
            "Errors": [{"Key": key, "Code": "AccessDenied"} for key in refused],
        }


def s3_backend(client: PagedS3Client, monkeypatch: pytest.MonkeyPatch, prefix: str = "") -> S3StorageBackend:
    backend = S3StorageBackend(bucket="bucket", prefix=prefix)
    monkeypatch.setattr(backend, "_client_or_raise", lambda: client)
    return backend


def tree_client(prefix: str = "") -> PagedS3Client:
    stored = f"{prefix}/" if prefix else ""
    return PagedS3Client(keys={f"{stored}{key}": FILE_SIZE for key in TREE_KEYS})


def build_tree_on_disk(root: Path) -> None:
    """Mirror TREE_KEYS on disk, plus symlinks that the S3 side has no way to express and the walk must ignore."""
    for key in TREE_KEYS:
        path = root / key
        if key.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_bytes(b"x" * FILE_SIZE)
    (root / "w" / "link").symlink_to(root / "w" / "a", target_is_directory=True)
    (root / "w" / "link.txt").symlink_to(root / "w" / "a.txt")


class ListOnlyBackend(StorageBackend):
    """A test double that implements only list_dir, over another backend, and counts the listings."""

    def __init__(self, source: StorageBackend) -> None:
        self.source: StorageBackend = source
        self.listed: list[str] = []

    @override
    async def list_dir(self, key: str) -> list[StorageEntry]:
        self.listed.append(key)
        return await self.source.list_dir(key)


def shape(entries: list[StorageEntry]) -> list[tuple[str, str, bool]]:
    return [(entry.name, entry.key, entry.is_dir) for entry in entries]
