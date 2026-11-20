from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import ClassVar

import anyio
import anyio.lowlevel
from pydantic import BaseModel, ConfigDict

from app.core.logging import logger
from app.services.agent_context_cache import invalidate_for_workspace_paths
from app.services.skill_provider import MAX_SKILL_SIZE, SkillFilePayload, parse_skill_md_frontmatter
from app.services.storage_runtime.agent_files import agent_storage_key
from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    StorageBackend,
    StorageReadChangedError,
    StorageReadLimitExceededError,
    StorageSnapshot,
    WriteCondition,
)
from app.services.storage_runtime.facade import get_storage_backend, guess_content_type

_TEXT = "text/plain; charset=utf-8"


class SkillFilesInvalidError(ValueError):
    """The files cannot form one skill folder; raised before any storage call (400)."""


class SkillFilesConflictError(Exception):
    """Storage holds something the import would clobber or could not put back; nothing stays written (409)."""


class _SkillFile(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True)

    path: str
    content: str


def _safe_part(part: str) -> bool:
    return part not in {"", ".", ".."} and "\\" not in part and "\x00" not in part


def _check_paths(paths: Iterable[str], *, shown_from: str = "") -> None:
    """Refuse unsafe paths, duplicates compared case-folded, and a file that is also another file's folder.

    An absolute path starts with an empty part, so it is unsafe too. Messages name each path without ``shown_from``.
    """
    folded: set[str] = set()
    checked: list[str] = []
    for path in paths:
        shown = path.removeprefix(shown_from)
        if not all(_safe_part(part) for part in path.split("/")):
            raise SkillFilesInvalidError(f"Unsafe skill file path: {shown!r}")
        if path.casefold() in folded:
            raise SkillFilesInvalidError(f"Duplicate skill file: {shown}")
        folded.add(path.casefold())
        checked.append(path)
    for path in checked:
        if any(parent.as_posix().casefold() in folded for parent in PurePosixPath(path).parents):
            raise SkillFilesInvalidError(f"Conflicting skill file path: {path.removeprefix(shown_from)}")


def _parse_files(payload: list[SkillFilePayload]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}

    def validated() -> Iterator[str]:
        # Each item is validated just before its path is checked, so the first bad item decides the error.
        for item in payload:
            file = _SkillFile.model_validate(item)
            files[file.path] = file.content.encode("utf-8")
            yield file.path

    _check_paths(validated())
    entry = files.get("SKILL.md")
    if entry is None:
        raise ValueError("Skill requires a root SKILL.md file")
    metadata = parse_skill_md_frontmatter(entry.decode("utf-8"))
    if not metadata.get("name", "").strip() or not metadata.get("description", "").strip():
        raise ValueError("SKILL.md requires valid frontmatter with a name and description")
    return files


async def _read_tree(storage: StorageBackend, prefix: str) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    pending = [prefix]
    while pending:
        for entry in await storage.list_dir(pending.pop()):
            if entry.is_dir:
                pending.append(entry.key)
            else:
                files[entry.key.removeprefix(f"{prefix}/")] = await storage.read_bytes(entry.key)
    return files


async def _replace_tree(storage: StorageBackend, prefix: str, files: dict[str, bytes]) -> None:
    await storage.delete_tree(prefix)
    for name, content in files.items():
        await storage.write_bytes(f"{prefix}/{name}", content, content_type=guess_content_type(name))


def _stage_local(skill_dir: Path, files: dict[str, bytes]) -> TemporaryDirectory[str]:
    if skill_dir.is_symlink():
        raise ValueError("Skill destination must not be a symlink")
    skill_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = TemporaryDirectory(prefix=f".{skill_dir.name}-install-", dir=skill_dir.parent)
    try:
        staging = Path(temporary.name) / "new"
        staging.mkdir()
        for name, content in files.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_bytes(content)
        backup = Path(temporary.name) / "previous"
        if skill_dir.exists():
            # Storage may share this local tree; retain an independent copy before publishing.
            _ = shutil.copytree(skill_dir, backup, symlinks=True)
    except BaseException:
        temporary.cleanup()
        raise
    return temporary


def _publish_local(skill_dir: Path, temporary: str) -> None:
    if skill_dir.exists():
        _ = skill_dir.rename(Path(temporary) / "discarded")
    _ = (Path(temporary) / "new").rename(skill_dir)


def _restore_local(skill_dir: Path, temporary: str) -> None:
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
    backup = Path(temporary) / "previous"
    if backup.exists():
        _ = backup.rename(skill_dir)


async def install_agent_skill(agent_id: uuid.UUID, skill_dir: Path, payload: list[SkillFilePayload]) -> None:
    files = _parse_files(payload)
    storage = get_storage_backend()
    prefix = agent_storage_key(agent_id, f"skills/{skill_dir.name}")
    previous = await _read_tree(storage, prefix)
    temporary = await anyio.to_thread.run_sync(_stage_local, skill_dir, files)
    try:
        with anyio.CancelScope(shield=True):
            try:
                await _replace_tree(storage, prefix, files)
                await anyio.to_thread.run_sync(_publish_local, skill_dir, temporary.name)
            except BaseException as install_error:
                # Rollback includes cancellation; shielding keeps cleanup from being interrupted.
                try:
                    await _replace_tree(storage, prefix, previous)
                except Exception as rollback_error:
                    raise BaseExceptionGroup(
                        "Skill publication and rollback failed", [install_error, rollback_error]
                    ) from None
                finally:
                    await anyio.to_thread.run_sync(_restore_local, skill_dir, temporary.name)
                raise
            await invalidate_for_workspace_paths(agent_id, f"skills/{skill_dir.name}")
    finally:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(temporary.cleanup)


@dataclass(frozen=True, slots=True)
class _Target:
    key: str
    data: bytes
    shown: str
    previous: StorageSnapshot | None  # None when nothing was at the key before the import


@dataclass(slots=True)
class _Write:
    target: _Target
    version: str | None = None  # None while the outcome of the write is unknown


async def _absent_folders(storage: StorageBackend, keys: Sequence[str], shown_from: str) -> list[str]:
    """The parent folders of ``keys`` that do not exist yet, deepest first; a non-folder at any of them conflicts."""
    folders = {parent.as_posix() for key in keys for parent in PurePosixPath(key).parents if parent.parts}
    absent: list[str] = []
    for folder in sorted(folders, key=lambda name: (-name.count("/"), name)):
        in_the_way = SkillFilesConflictError(f"A file is in the way of skill folder: {folder.removeprefix(shown_from)}")
        try:
            # Bounded: the local backend hashes a file to version it, and a large file in the way needs no read.
            version = await storage.get_version_bounded(folder, MAX_SKILL_SIZE)
        except StorageReadLimitExceededError, FileNotFoundError:
            # The local backend raises FileNotFoundError for an entry that is neither a file nor a folder.
            raise in_the_way from None
        if version.exists and not version.is_dir:
            raise in_the_way
        if not version.exists:
            absent.append(folder)
    return absent


async def _previous(storage: StorageBackend, key: str, shown: str) -> StorageSnapshot | None:
    """The bytes and version at ``key``, or None when nothing is there; anything but a restorable file conflicts."""
    try:
        version = await storage.get_version_bounded(key, MAX_SKILL_SIZE)
        # S3 versions never report a prefix as a folder, so an absent key is also asked whether it is one.
        if version.is_dir or (not version.exists and await storage.is_dir(key)):
            raise SkillFilesConflictError(f"A folder is in the way of skill file: {shown}")
        if not version.exists:
            return None
        # One read supplies both the bytes an undo restores and the token every later condition compares.
        return await storage.read_snapshot_bounded(key, MAX_SKILL_SIZE)
    except StorageReadLimitExceededError:
        raise SkillFilesConflictError(f"Import would overwrite a file too large to restore: {shown}") from None
    except FileNotFoundError, StorageReadChangedError:
        # Not a regular file, or replaced between the version probe and the read.
        raise SkillFilesConflictError(f"Skill file changed during import: {shown}") from None


async def _publish(storage: StorageBackend, target: _Target) -> ConditionalWriteResult:
    if target.previous is None:
        return await storage.create_bytes_if_absent(target.key, target.data, content_type=_TEXT)
    condition = WriteCondition(version_token=target.previous.version.token)
    return await storage.write_bytes_if_match(target.key, target.data, condition=condition, content_type=_TEXT)


async def _written_version(storage: StorageBackend, key: str, result: ConditionalWriteResult) -> str:
    if result.current_version is not None:
        return result.current_version.token
    # Race: a backend that reports success without the new version is read back, and a writer landing in between
    # hands us its version instead, so an undo would replace or delete that writer's bytes rather than ours.
    return (await storage.get_version(key)).token


async def _undo_write(storage: StorageBackend, write: _Write) -> bool:
    """Put back what one write replaced; False when the key no longer holds what the import wrote."""
    target, version = write.target, write.version
    if version is None:
        # The write raised, so whether it landed is unknown: undo only a file that differs from the snapshot, against
        # the version seen now. A writer landing between the failed write and this read is undone with it; the
        # alternative leaves a torn local in-place rewrite, or a create whose bytes landed before the error.
        current = await storage.get_version(target.key)
        if not current.exists:
            return target.previous is None  # neither write removes a file, so someone else deleted it
        if current.is_dir:
            return False
        if target.previous is not None and current.token == target.previous.version.token:
            return True
        version = current.token
    condition = WriteCondition(version_token=version)
    if target.previous is None:
        # Race: FallbackStorageBackend deletes the legacy copy too, so a file the guest wrote at this key on its own
        # disk between the import's create and this undo goes with it.
        result = await storage.delete_if_match(target.key, condition=condition)
    else:
        # Restore, never delete: behind FallbackStorageBackend(promote=False) a key only the legacy side held was
        # written to the primary, and a delete would remove the legacy file too.
        result = await storage.write_bytes_if_match(
            target.key, target.previous.data, condition=condition, content_type=_TEXT
        )
    return result.ok


async def _undo(storage: StorageBackend, writes: list[_Write], created_folders: list[str]) -> None:
    """Undo the writes newest first, then remove the folders the import created, deepest first.

    Newest first walks back through states the forward pass already held, so a metered backend needs no more room to
    undo than it granted to write, unless another writer took the room an undo freed; that restore then fails and the
    import's bytes stay. A step that fails is logged as an error, one that finds someone else's bytes as a warning,
    and the rest still run.
    """
    for write in reversed(writes):
        key = write.target.key
        try:
            undone = await _undo_write(storage, write)
        except Exception as error:
            logger.error(f"Skill import rollback left {key} as it is: {type(error).__name__}: {error}")
            continue
        if not undone:
            logger.warning(f"Skill import rollback left {key} as it is: it no longer holds what the import wrote")
    for folder in created_folders:
        try:
            await storage.remove_empty_directory(folder)
        except Exception as error:
            logger.warning(f"Skill import rollback left folder {folder}: {type(error).__name__}: {error}")


async def write_skill_files(storage: StorageBackend, files: Sequence[tuple[str, bytes]], *, folder: str) -> list[str]:
    """Write a skill folder's files all or nothing, merging into whatever the folder already holds.

    ``files`` pairs full storage keys below ``folder`` (the skill folder's key), already validated and placed by the
    caller, with their bytes; messages name paths relative to ``folder``. Every check runs
    before the first write: the paths (``SkillFilesInvalidError``, before any storage call), then a snapshot of each
    target and parent folder (``SkillFilesConflictError``). Each target is written only while it still matches its
    snapshot, and identical bytes are skipped. On any exception, cancellation included, every write is undone and the
    folders this call created are removed, then the original exception propagates; a target that changed after the
    snapshot raises ``SkillFilesConflictError`` after that undo.

    Returns every key of ``files`` in order, skipped ones included: on success each holds exactly its bytes.
    """
    keys = [key for key, _data in files]
    shown_from = f"{folder.rstrip('/')}/"
    _check_paths(keys, shown_from=shown_from)
    created_folders = await _absent_folders(storage, keys, shown_from)
    targets: list[_Target] = []
    for key, data in files:
        shown = key.removeprefix(shown_from)
        targets.append(_Target(key, data, shown, await _previous(storage, key, shown)))
    writes: list[_Write] = []
    try:
        for target in targets:
            if target.previous is not None and target.previous.data == target.data:
                continue
            write = _Write(target)
            writes.append(write)  # recorded before the call, so a write whose outcome is unknown is still undone
            # Shielded so a write that landed is always recorded with its version; cancellation is taken below.
            with anyio.CancelScope(shield=True):
                result = await _publish(storage, target)
                if not result.ok:
                    _ = writes.pop()  # nothing of this import landed there, and the other writer's change stays
                    raise SkillFilesConflictError(f"Skill file changed during import: {target.shown}")
                write.version = await _written_version(storage, target.key, result)
            await anyio.lowlevel.checkpoint()
    except BaseException:
        with anyio.CancelScope(shield=True):
            await _undo(storage, writes, created_folders)
        raise
    return keys
