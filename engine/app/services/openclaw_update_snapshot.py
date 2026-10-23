"""Full-state snapshot and idempotent restore for OpenClaw cutovers.

Directory layout beside the agent directory: ``_upgrade_snapshots/<run>`` is the
committed snapshot; ``<agent>.rollback-<run8>`` holds the discarded new state
between the two rename steps of a restore. Every function is safe to repeat.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import UUID


def snapshot_path(agent_dir: Path, run_id: UUID) -> Path:
    return agent_dir.parent / "_upgrade_snapshots" / str(run_id)


def _discard_path(agent_dir: Path, run_id: UUID) -> Path:
    return agent_dir.with_name(f"{agent_dir.name}.rollback-{str(run_id)[:8]}")


def commit_snapshot(agent_dir: Path, run_id: UUID) -> Path:
    """Copy the whole agent directory, then publish it with one atomic rename."""
    final = snapshot_path(agent_dir, run_id)
    if final.exists():
        return final
    staging = final.with_name(f"{final.name}.tmp")
    final.parent.mkdir(parents=True, exist_ok=True)
    if staging.exists():
        shutil.rmtree(staging)
    _ = shutil.copytree(agent_dir, staging, symlinks=True)
    staging.rename(final)
    return final


def restore_snapshot(agent_dir: Path, run_id: UUID) -> bool:
    """Swap the committed snapshot back in; ``False`` when none remains to restore."""
    final = snapshot_path(agent_dir, run_id)
    if not final.exists():
        return False
    discard = _discard_path(agent_dir, run_id)
    if discard.exists() and agent_dir.exists():
        shutil.rmtree(discard)
    if agent_dir.exists():
        agent_dir.rename(discard)
    final.rename(agent_dir)
    return True


def drop_snapshot(agent_dir: Path, run_id: UUID) -> None:
    """Remove the committed snapshot and any discarded new state."""
    final = snapshot_path(agent_dir, run_id)
    for path in (final, final.with_name(f"{final.name}.tmp"), _discard_path(agent_dir, run_id)):
        if path.exists():
            shutil.rmtree(path)
