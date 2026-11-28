"""Refuse a large copy onto the guest data volume before it can fill the host disk.

Every engine path that stages a big copy there calls `require_headroom` first: the state snapshot backup, the snapshot
restore, the OpenClaw upgrade and recovery snapshots and the deleted-agent archive. Contract: docs/agent-state-snapshots.md.
"""

import errno
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from app.config import get_settings
from app.core.logging import logger


class DiskUsage(Protocol):
    @property
    def free(self) -> int: ...


# The seam tests replace: the usage of the volume that holds a path.
disk_usage: Callable[[Path], DiskUsage] = shutil.disk_usage


def require_headroom(path: Path, expected_bytes: int = 0) -> None:
    """Raise `OSError` with `ENOSPC` unless the volume holding `path` has the configured floor free, plus `expected_bytes`.

    `path` need not exist yet: its nearest existing ancestor names the volume. A floor of 0 turns the check off.
    `expected_bytes` is what the caller already knows it is about to write; pass 0 when that is not known.
    """
    floor = get_settings().HOST_DISK_HEADROOM_BYTES
    if floor == 0:
        return
    required = floor + expected_bytes
    free = disk_usage(_existing_ancestor(path)).free
    if free < required:
        logger.warning("[HostDisk] refused a copy: {} bytes free, {} required", free, required)
        raise OSError(errno.ENOSPC, f"Host disk headroom is short: {free} bytes free, {required} required")


def _existing_ancestor(path: Path) -> Path:
    return next(candidate for candidate in (path, *path.parents) if candidate.exists())
