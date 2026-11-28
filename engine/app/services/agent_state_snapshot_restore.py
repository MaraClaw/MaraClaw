"""Restore an agent's newest usable state snapshot before a start on a host that has no OpenClaw state.

Contract: docs/agent-state-snapshots.md. Transient failures, a short or full host disk included, fail the start with
nothing placed; a snapshot this image must not restore gets a held marker and the start goes on fresh.
"""

import errno
import os
import re
import shutil
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import Final
from uuid import UUID

from anyio import to_thread
from pywhales import ClientNotFoundError, DockerClient
from pywhales.exceptions import DockerException

from app.config import get_settings
from app.core.json_types import is_any_list, is_str_dict, json_loads_value
from app.core.logging import logger
from app.records.agent import AgentRecord
from app.services.agent_state_snapshot import (
    ARCHIVE_NAME,
    CONTAINER_TMP,
    GUEST_STATE_DIR,
    STATE_DBS,
    STORAGE_ERRORS,
    SnapshotBusyError,
    SnapshotName,
    has_host_state,
    list_snapshots,
    new_work_dir,
    placeable,
    run_one_off,
    snapshot_lock,
    snapshot_store,
    tenant_token,
    walk_covered,
    work_root,
)
from app.services.agent_state_snapshot_archive import ASSET_PAYLOAD
from app.services.docker_capacity import DockerBusyError, run_docker
from app.services.host_disk_headroom import require_headroom
from app.services.storage_runtime.base import StorageBackend
from app.services.storage_runtime.channel_files import delete_guest_file, link_guest_file
from app.services.workspace_mirror_scope import WalkLimitError

RESTORE_ROLE: Final = "restore"
DOWNLOAD_CHUNK_BYTES: Final = 8 * 1024 * 1024
REPORT_MAX_BYTES: Final = 1024 * 1024
_KILLED = 137
_RESTORE_SCRIPT: Final = (
    "openclaw --version > /work/version.txt 2> /work/version.log || exit 70; "
    f"timeout -s KILL 120 openclaw backup restore /work/in/{ARCHIVE_NAME} --target /work/stage --json "
    "> /work/result.json 2> /work/restore.log; echo $? > /work/exit_code"
)
_RESTORE_ENVS: Final = {
    "HOME": CONTAINER_TMP,
    "OPENCLAW_STATE_DIR": f"{CONTAINER_TMP}/openclaw-state",
    "TMPDIR": CONTAINER_TMP,
}
_IMAGE_VERSION = re.compile(r"OpenClaw (\S+)")
_VERSION = re.compile(r"(\d+(?:\.\d+)+)(?:[-+][0-9A-Za-z.-]+)?")
_NO_SPACE = re.compile(r"\bENOSPC\b|no space left on device", re.IGNORECASE)
_BLOCKED_PATH_ERRNOS = frozenset({errno.ELOOP, errno.ENOTDIR})


class AgentStateRestoreError(OSError):
    """The snapshot could not be restored now: the start fails with nothing placed, and a later start retries."""


class SnapshotRefusedError(Exception):
    """This image must not restore the snapshot: its archive, manifest or runtime version is unusable here."""


async def restore_agent_state(
    client: DockerClient, agent: AgentRecord, agent_dir: Path, guest_image: str
) -> frozenset[str]:
    """Place the agent's newest usable snapshot when the host has no OpenClaw state; return the relative paths placed.

    The caller holds the exclusive setup lock and goes on to pull the mirror and materialize storage, which must leave
    the returned paths alone. Without a snapshot store, with host state, or with no usable snapshot nothing happens.
    Any local failure, a short or full host disk included, is an `AgentStateRestoreError`, never a refusal.
    """
    store = snapshot_store()
    if store is None or await to_thread.run_sync(has_host_state, agent_dir):
        return frozenset()
    try:
        async with snapshot_lock(agent_dir):
            return await _restore(store, client, agent, agent_dir, guest_image)
    except SnapshotBusyError as exc:
        raise AgentStateRestoreError("A state snapshot of this agent is being taken or deleted") from exc
    except AgentStateRestoreError:
        raise
    except (OSError, WalkLimitError) as exc:
        raise AgentStateRestoreError(f"A local step of the restore failed: {type(exc).__name__}") from exc


async def _restore(
    store: StorageBackend, client: DockerClient, agent: AgentRecord, agent_dir: Path, guest_image: str
) -> frozenset[str]:
    try:
        listing = await list_snapshots(store, agent.id)
    except STORAGE_ERRORS as exc:
        raise AgentStateRestoreError(f"Listing the state snapshots failed: {type(exc).__name__}") from exc
    chosen = listing.restorable(tenant_token(agent.tenant_id))
    if chosen is None:
        return frozenset()
    max_bytes = get_settings().AGENT_STATE_SNAPSHOT_MAX_BYTES
    if listing.sizes[chosen] > max_bytes:
        await to_thread.run_sync(require_headroom, work_root(agent_dir.parent))
        await _hold(store, agent.id, chosen, f"the archive is over the {max_bytes} byte limit")
        return frozenset()
    work = await to_thread.run_sync(_new_restore_dir, agent_dir.parent, agent.id, listing.sizes[chosen])
    try:
        await _download(store, chosen.key(agent.id), work / "in" / ARCHIVE_NAME, max_bytes)
        try:
            await run_docker(_restore_container, client, guest_image, agent.id, work)
        except (DockerBusyError, ClientNotFoundError, DockerException) as exc:
            raise AgentStateRestoreError(f"The restore container failed: {type(exc).__name__}") from exc
        try:
            payload = await to_thread.run_sync(restored_payload, work)
        except SnapshotRefusedError as exc:
            await to_thread.run_sync(require_headroom, work)
            await _hold(store, agent.id, chosen, str(exc))
            return frozenset()
        placed = await to_thread.run_sync(place_restored_state, payload, agent_dir)
    finally:
        await to_thread.run_sync(partial(shutil.rmtree, work.parent, ignore_errors=True))
    logger.info("[StateSnapshot] restored agent={} snapshot={} files={}", agent.id, chosen.file_name, len(placed))
    return placed


def _new_restore_dir(agents: Path, agent_id: UUID, archive_bytes: int) -> Path:
    """A fresh work directory, after removing what a crashed restore of the same agent left (this flock is held).

    The host disk is checked for the archive about to be downloaded before anything is created.
    """
    shutil.rmtree(work_root(agents) / RESTORE_ROLE / str(agent_id), ignore_errors=True)
    require_headroom(work_root(agents), archive_bytes)
    work = new_work_dir(agents, RESTORE_ROLE, str(agent_id))
    try:
        (work / "in").mkdir()
    except OSError:
        shutil.rmtree(work.parent, ignore_errors=True)
        raise
    return work


async def _download(store: StorageBackend, key: str, target: Path, max_bytes: int) -> None:
    """Copy the archive into the work directory in ranged reads; the process temp directory is invisible to Docker."""
    with target.open("xb") as stream:
        offset = 0
        while True:
            try:
                chunk = await store.read_bytes_bounded(key, DOWNLOAD_CHUNK_BYTES, offset=offset)
            except STORAGE_ERRORS as exc:
                raise AgentStateRestoreError(f"Downloading the state snapshot failed: {type(exc).__name__}") from exc
            offset += len(chunk)
            if offset > max_bytes:
                raise AgentStateRestoreError("The state snapshot grew past the size limit while it was downloaded")
            _ = await to_thread.run_sync(stream.write, chunk)
            if len(chunk) < DOWNLOAD_CHUNK_BYTES:
                return


def _restore_container(client: DockerClient, image: str, agent_id: UUID, work: Path) -> None:
    _ = run_one_off(
        client,
        image,
        _RESTORE_SCRIPT,
        name=f"maraclaw-state-restore-{agent_id}",
        role=RESTORE_ROLE,
        agent_id=agent_id,
        mounts={work: "/work"},
        envs=_RESTORE_ENVS,
    )


def _read_report(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        return stream.read(REPORT_MAX_BYTES).decode(errors="replace")


def _version(text: str) -> tuple[int, ...] | None:
    match = _VERSION.fullmatch(text)
    return None if match is None else tuple(int(part) for part in match.group(1).split("."))


def restored_payload(work: Path) -> Path:
    """The restored state directory below the stage, after the restore's outcome, manifest and version check out.

    `AgentStateRestoreError` when the restore never finished, `SnapshotRefusedError` when this image must not use it. A
    restore that exited non-zero on a host disk that is now short may have run out of room, so it is not a refusal.
    """
    try:
        exit_code = int(_read_report(work / "exit_code").strip())
    except (OSError, ValueError) as exc:
        raise AgentStateRestoreError("The restore container left no exit code") from exc
    if exit_code == _KILLED:
        raise AgentStateRestoreError("The restore was killed before it finished")
    if exit_code != 0:
        for path in (work / "restore.log", work / "result.json"):
            with suppress(FileNotFoundError):
                if _NO_SPACE.search(_read_report(path)):
                    raise AgentStateRestoreError("The restore container ran out of host disk space")
        raise SnapshotRefusedError(f"openclaw backup restore rejected the archive (exit code {exit_code})")
    try:
        image_line = _IMAGE_VERSION.search(_read_report(work / "version.txt"))
        result = json_loads_value(_read_report(work / "result.json"))
    except (FileNotFoundError, ValueError) as exc:
        raise SnapshotRefusedError(f"The restore reports are unreadable: {type(exc).__name__}") from exc
    image_version = None if image_line is None else _version(image_line.group(1))
    if image_version is None:
        raise SnapshotRefusedError("The image's OpenClaw version is unparseable")
    if not is_str_dict(result) or result.get("ok") is not True:
        raise SnapshotRefusedError("The restore did not report success")
    root = result.get("archiveRoot")
    if not isinstance(root, str) or not root or root in (".", "..") or "/" in root:
        raise SnapshotRefusedError("The restore reported no usable archive root")
    try:
        manifest = json_loads_value(_read_report(work / "stage" / root / "manifest.json"))
    except (FileNotFoundError, ValueError) as exc:
        raise SnapshotRefusedError(f"The archive manifest is unreadable: {type(exc).__name__}") from exc
    if not is_str_dict(manifest):
        raise SnapshotRefusedError("The archive manifest is not an object")
    runtime = manifest.get("runtimeVersion")
    snapshot_version = _version(runtime) if isinstance(runtime, str) else None
    if snapshot_version is None:
        raise SnapshotRefusedError("The snapshot's OpenClaw version is unparseable")
    if snapshot_version > image_version:
        raise SnapshotRefusedError(f"The snapshot was taken by OpenClaw {runtime}, newer than the image's")
    assets = manifest.get("assets")
    expected = {"kind": "state", "sourcePath": GUEST_STATE_DIR, "archivePath": f"{root}/{ASSET_PAYLOAD}"}
    if not is_any_list(assets) or len(assets) != 1 or not is_str_dict(asset := assets[0]):
        raise SnapshotRefusedError("The archive does not hold exactly one asset")
    if any(asset.get(field) != value for field, value in expected.items()):
        raise SnapshotRefusedError("The archive holds an asset outside the agent state directory")
    symlinks = result.get("symlinkCount")
    if isinstance(symlinks, int) and symlinks > 0:
        logger.warning("[StateSnapshot] {} symlinks in the snapshot are not restored", symlinks)
    return work / "stage" / root / ASSET_PAYLOAD


def place_restored_state(payload: Path, agent_dir: Path) -> frozenset[str]:
    """Hard-link every placeable regular file of the restored payload where nothing sits in the agent directory yet.

    Links keep the archived inode and mode; nothing existing is replaced and symlinked parents are refused. The two
    OpenClaw databases go last, so an interrupted placement leaves the host without state and the next start restores
    again. A failure unlinks what this call placed and raises.
    """
    files = walk_covered(payload, placeable)
    agent_dir.mkdir(parents=True, exist_ok=True)
    placed: list[str] = []
    try:
        for file in sorted(files, key=lambda file: file.rel in STATE_DBS):
            try:
                linked = link_guest_file(payload, agent_dir, file.rel)
            except OSError as exc:
                if exc.errno not in _BLOCKED_PATH_ERRNOS:
                    raise
                logger.warning("[StateSnapshot] left {} alone: a symlink or file blocks its path", file.rel)
                continue
            if linked:
                placed.append(file.rel)
    except BaseException:
        for rel in placed:
            with suppress(OSError):
                delete_guest_file(agent_dir, rel)
        raise
    return frozenset(placed)


async def _hold(store: StorageBackend, agent_id: UUID, name: SnapshotName, reason: str) -> None:
    """Mark the snapshot held so no later start tries it again; an operator releases it by deleting the marker."""
    try:
        _ = await store.create_bytes_if_absent(name.held_key(agent_id), b"", content_type="text/plain")
    except STORAGE_ERRORS as exc:
        raise AgentStateRestoreError(f"Holding the state snapshot failed: {type(exc).__name__}") from exc
    logger.warning("[StateSnapshot] held snapshot={} agent={}, starting fresh: {}", name.file_name, agent_id, reason)
