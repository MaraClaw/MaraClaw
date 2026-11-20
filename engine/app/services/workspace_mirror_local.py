"""Contained probes of one agent directory for the workspace mirror: its generation marker and path states.

Everything here runs on worker threads. The guest controls the tree below the agent directory, so every probe goes
through directory descriptors and never follows a symlink.
"""

import errno
import os
import stat
from collections.abc import Iterable
from pathlib import Path
from uuid import UUID, uuid4

from app.core.logging import logger
from app.services.storage_runtime.channel_files import GuestFileChangedError, lstat_guest_path

MARKER = ".maraclaw-mirror-generation"
_MARKER_BYTES = 64
# A new generation's marker carries this suffix until one pass of it finished; only then is its manifest a base.
_PENDING = " pending"
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_MARKER_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK
# What a contained local operation reports when the host itself is in trouble; the pass stops. Any other failure
# comes from what the guest put on that one path (moved, replaced, blocked, made unreadable, named too long), so
# only that path waits for the next pass.
_HOST_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS, errno.ENOMEM, errno.ENFILE, errno.EMFILE})


def unsettled(exc: OSError) -> bool:
    """Whether a contained local operation failed because of what the guest put on the path, not a host fault."""
    # The contained helpers refuse a non-file destination with an errno-less OSError.
    return isinstance(exc, GuestFileChangedError) or exc.errno not in _HOST_ERRNOS


def _read_marker(directory: int) -> tuple[UUID, bool] | None:
    """The marker's generation and whether a pass of it finished; None unless a valid marker file sits there."""
    try:
        fd = os.open(MARKER, os.O_RDONLY | _MARKER_FLAGS, dir_fd=directory)
    except OSError as exc:
        if unsettled(exc):
            return None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, _MARKER_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > _MARKER_BYTES:
        return None
    try:
        text = raw.decode("ascii").strip()
        return UUID(text.removesuffix(_PENDING)), not text.endswith(_PENDING)
    except ValueError:
        return None


def _move_aside(directory: int, agent_dir: Path) -> None:
    """Move whatever invalid entry sits on the marker path aside under a dot name, so no guest data is deleted."""
    aside = f"{MARKER}.displaced-{uuid4().hex}"
    try:
        os.rename(MARKER, aside, src_dir_fd=directory, dst_dir_fd=directory)
    except FileNotFoundError:
        return
    logger.warning(f"Workspace mirror moved an invalid generation marker in {agent_dir} aside to {aside}")


def read_or_create_generation(agent_dir: Path) -> tuple[UUID, bool]:
    """The directory's mirror generation and whether a pass of it already finished, so its manifest is a base.

    A directory without a valid marker starts a new, pending generation; whatever sat on the marker path is moved
    aside first.
    """
    directory = os.open(agent_dir, _DIRECTORY)
    try:
        current = _read_marker(directory)
        if current is not None:
            return current
        _move_aside(directory, agent_dir)
        generation = uuid4()
        fd = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory)
        with os.fdopen(fd, "wb") as stream:
            _ = stream.write(f"{generation}{_PENDING}".encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
        return generation, False
    finally:
        os.close(directory)


def complete_generation(agent_dir: Path, generation: UUID) -> None:
    """Record that a pass of the pending `generation` finished; a marker the guest changed meanwhile stays as it is."""
    complete = str(generation).encode("ascii")
    directory = os.open(agent_dir, _DIRECTORY)
    try:
        try:
            fd = os.open(MARKER, os.O_RDWR | _MARKER_FLAGS, dir_fd=directory)
        except OSError as exc:
            if unsettled(exc):
                return
            raise
        try:
            pending = complete + _PENDING.encode("ascii")
            if stat.S_ISREG(os.fstat(fd).st_mode) and os.read(fd, _MARKER_BYTES + 1) == pending:
                os.ftruncate(fd, len(complete))
                os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def blocked_prefixes(agent_dir: Path, directories: Iterable[str]) -> tuple[str, ...]:
    """Prefixes of the scope directories that exist as something other than a real directory.

    The walk silently skips such a directory, which would make everything below it look deleted.
    """
    blocked: list[str] = []
    for name in directories:
        try:
            info = lstat_guest_path(agent_dir, name)
        except FileNotFoundError:
            continue
        except OSError as exc:
            if not unsettled(exc):
                raise
            blocked.append(f"{name}/")
            continue
        if not stat.S_ISDIR(info.st_mode):
            blocked.append(f"{name}/")
    return tuple(blocked)


def proven_absent(agent_dir: Path, rel: str) -> bool:
    """True only when nothing at all sits at `rel` and no symlink or file blocks a parent of it."""
    try:
        _ = lstat_guest_path(agent_dir, rel)
    except FileNotFoundError:
        return True
    except OSError as exc:
        if not unsettled(exc):
            raise
    return False


def unproven_absences(agent_dir: Path, paths: Iterable[str]) -> set[str]:
    """The paths the walk did not list although something, a symlink or a blocked parent, may still sit there."""
    return {rel for rel in paths if not proven_absent(agent_dir, rel)}
