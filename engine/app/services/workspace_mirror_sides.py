"""What one workspace mirror pass finds on each side of an agent, and the action it plans for every path."""

from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from uuid import UUID

import anyio

from app.dao.workspace_mirror_entry_dao import workspace_mirror_entry_dao
from app.records.workspace_mirror_entry import WorkspaceMirrorEntryRecord
from app.services.storage_runtime.agent_files import agent_storage_key
from app.services.storage_runtime.base import StorageBackend
from app.services.workspace_mirror_local import blocked_prefixes, unproven_absences
from app.services.workspace_mirror_plan import Action, Remote, decide, freeze_reason, scope_directories
from app.services.workspace_mirror_scope import (
    ROOT_FILES,
    LocalFile,
    case_collisions,
    in_scope,
    scope_storage_prefixes,
    walk_local,
)


@dataclass(frozen=True, slots=True)
class Sides:
    """The guest's files, storage's objects and the manifest of one agent, as one pass gathered them."""

    generation: UUID
    local: dict[str, LocalFile]
    remote: dict[str, Remote]
    manifest: dict[str, WorkspaceMirrorEntryRecord]

    def base(self, rel: str) -> WorkspaceMirrorEntryRecord | None:
        entry = self.manifest.get(rel)
        return entry if entry is not None and entry.generation == self.generation else None


def moment(now_ns: int) -> datetime:
    return datetime.fromtimestamp(now_ns / 1_000_000_000, UTC)


def recorded_count(manifest: dict[str, WorkspaceMirrorEntryRecord], generation: UUID) -> int:
    """In-scope paths of this generation the manifest saw in storage: what an empty listing would call deleted."""
    return sum(
        1
        for rel, entry in manifest.items()
        if entry.generation == generation and entry.remote_etag is not None and in_scope(rel)
    )


async def _stored(store: StorageBackend, agent_id: UUID) -> AsyncGenerator[tuple[str, Remote]]:
    """In-scope objects of one agent: a flat listing per scope prefix and a HEAD per root file."""
    prefix = agent_storage_key(agent_id) + "/"
    for scope_prefix in scope_storage_prefixes():
        async with aclosing(store.list_files_flat(prefix + scope_prefix)) as entries:
            async for entry in entries:
                rel = entry.key.removeprefix(prefix)
                if in_scope(rel):
                    yield rel, Remote(entry.etag, entry.size, entry.modified_at)
    for name in sorted(ROOT_FILES):
        version = await store.get_version(agent_storage_key(agent_id, name))
        if version.exists and not version.is_dir:
            yield name, Remote(version.etag, version.size, version.modified_at)


async def stores_nothing(store: StorageBackend, agent_id: UUID) -> bool:
    """Whether storage lists no in-scope object of the agent at all; stops at the first one it finds."""
    async with aclosing(_stored(store, agent_id)) as objects:
        return await anext(objects, None) is None


async def gather(
    agent_id: UUID, agent_dir: Path, store: StorageBackend, generation: UUID, budget_seconds: float
) -> Sides:
    walk = partial(walk_local, agent_dir, deadline_seconds=budget_seconds / 2)
    local = {file.rel: file for file in await anyio.to_thread.run_sync(walk)}
    remote = {rel: found async for rel, found in _stored(store, agent_id)}
    manifest = await workspace_mirror_entry_dao.list_for_agent(agent_id)
    return Sides(generation, local, remote, manifest)


async def frozen_paths(agent_dir: Path, sides: Sides, paths: list[str]) -> dict[str, str]:
    """Paths that must sit this pass out, before anything about them is decided, and why."""
    blocked = await anyio.to_thread.run_sync(blocked_prefixes, agent_dir, scope_directories())
    frozen = {rel: "scope directory is not a real directory" for rel in paths if rel.startswith(blocked)}
    frozen |= dict.fromkeys(case_collisions(paths), "differs from another path only by case")
    unlisted = [rel for rel in paths if rel not in sides.local and rel not in frozen]
    unproven = await anyio.to_thread.run_sync(unproven_absences, agent_dir, unlisted)
    frozen |= dict.fromkeys(unproven, "a symlink, a non-file or an unreadable parent sits on the guest path")
    return frozen


def plan(sides: Sides, paths: list[str], frozen: dict[str, str], now_ns: int) -> dict[str, Action]:
    """The action of every path that needs one; a path found unstable joins `frozen` instead."""
    actions: dict[str, Action] = {}
    now = moment(now_ns)
    for rel in paths:
        if rel in frozen:
            continue
        local, remote = sides.local.get(rel), sides.remote.get(rel)
        reason = freeze_reason(local, remote, sides.base(rel), now_ns=now_ns, settle_local=True)
        if reason is not None:
            frozen[rel] = reason
            continue
        action = decide(rel, sides.base(rel), local, remote, now=now)
        if action is not Action.NOOP:
            actions[rel] = action
    return actions
