"""Local batch delete and copy: each key is checked and changed through the contained descriptors of its own path.

A listed version is the file's modification time and size (``local_version_stamp``), which a directory scan reads without
opening the file. The check and the change use the same open entry, so a link planted on the way is refused as always.
"""

import os
import stat
from collections.abc import Sequence
from contextlib import ExitStack, suppress
from pathlib import Path

from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    CopyRequest,
    StorageLinkRefusedError,
    StorageVersion,
    deleted_result,
    kept_result,
)
from app.services.storage_runtime.local_contained import (
    contained_parent,
    local_version_stamp,
    open_regular_file,
    token_stamp,
)
from app.services.storage_runtime.local_create import write_contained_file
from app.services.storage_runtime.upload_source import UploadSource
from app.services.storage_runtime.utils import normalize_storage_key


def delete_batch(root: Path, versions: Sequence[StorageVersion]) -> dict[str, ConditionalWriteResult]:
    """Unlink each file that still has its listed stamp; a changed or missing file is left as it is."""
    return {version.key: _delete_if_unchanged(root, version) for version in versions}


def copy_batch(root: Path, copies: Sequence[CopyRequest]) -> dict[str, ConditionalWriteResult]:
    """Copy each file that still has its listed stamp, streaming from the descriptor that was checked."""
    return {copy.source.key: _copy_if_unchanged(root, copy) for copy in copies}


def _delete_if_unchanged(root: Path, version: StorageVersion) -> ConditionalWriteResult:
    with (
        suppress(FileNotFoundError, NotADirectoryError),
        contained_parent(root, normalize_storage_key(version.key)) as (
            directory,
            name,
        ),
    ):
        if name == ".":
            return kept_result(version.key, exists=True)  # the storage root is never an entry to delete
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise StorageLinkRefusedError
        if not stat.S_ISREG(info.st_mode) or local_version_stamp(info) != token_stamp(version.version_id):
            return kept_result(version.key, exists=True)
        os.unlink(name, dir_fd=directory)
        return deleted_result(version.key)
    return kept_result(version.key, exists=False)


def _copy_if_unchanged(root: Path, copy: CopyRequest) -> ConditionalWriteResult:
    with ExitStack() as stack:
        try:
            stream = stack.enter_context(open_regular_file(root, normalize_storage_key(copy.source.key)))
        except FileNotFoundError:
            return kept_result(copy.source.key, exists=False)
        info = os.fstat(stream.fileno())
        if local_version_stamp(info) != token_stamp(copy.source.version_id):
            return kept_result(copy.source.key, exists=True)
        write_contained_file(root, normalize_storage_key(copy.destination_key), UploadSource(stream, info.st_size, ""))
    return ConditionalWriteResult(
        ok=True,
        current_version=StorageVersion(key=copy.destination_key, exists=True, is_dir=False, size=info.st_size),
    )
