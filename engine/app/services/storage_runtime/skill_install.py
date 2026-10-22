from __future__ import annotations

import shutil
import uuid
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import ClassVar

import anyio
from pydantic import BaseModel, ConfigDict

from app.services.agent_context_cache import invalidate_for_workspace_paths
from app.services.skill_provider import SkillFilePayload, parse_skill_md_frontmatter
from app.services.storage_runtime.agent_files import agent_storage_key
from app.services.storage_runtime.base import StorageBackend
from app.services.storage_runtime.facade import get_storage_backend, guess_content_type


class _SkillFile(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True)

    path: str
    content: str


def _parse_files(payload: list[SkillFilePayload]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    paths: set[str] = set()
    for item in payload:
        file = _SkillFile.model_validate(item)
        path = PurePosixPath(file.path)
        if (
            path.is_absolute()
            or "\\" in file.path
            or "\x00" in file.path
            or any(part in {"", ".", ".."} for part in file.path.split("/"))
        ):
            raise ValueError(f"Unsafe skill file path: {file.path!r}")
        folded = file.path.casefold()
        if folded in paths:
            raise ValueError(f"Duplicate skill file: {file.path}")
        paths.add(folded)
        files[file.path] = file.content.encode("utf-8")
    for name in files:
        if any(parent.as_posix().casefold() in paths for parent in PurePosixPath(name).parents):
            raise ValueError(f"Conflicting skill file path: {name}")
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
