import os
import shutil
from pathlib import Path
from uuid import UUID

from app.config import get_settings
from app.records.agent_deletion_cleanup import AgentDeletionCleanupRecord


def deletion_archive_path(agent_id: UUID) -> Path:
    settings = get_settings()
    root = Path(settings.STORAGE_LOCAL_ROOT or settings.AGENT_DATA_DIR)
    return (root / "_archived" / f"{agent_id}_deleted").absolute()


def publish_deletion_archive(job: AgentDeletionCleanupRecord) -> None:
    source, target = job.workspace_path, job.archive_path
    marker = str(job.agent_id)
    if target.exists():
        if (target / ".deletion-cleanup").read_text() != marker:
            raise OSError("Deleted-agent archive identity conflict")
        if (target / "task_history.json").read_text(encoding="utf-8") != job.task_history:
            raise OSError("Deleted-agent archive history conflict")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_name(f".{target.name}.pending")
        if staging.exists():
            shutil.rmtree(staging)
        if source.is_symlink():
            raise OSError("Deleted-agent workspace cannot be a symlink")
        if source.exists():
            shutil.copytree(source, staging, symlinks=True)
        else:
            staging.mkdir()
        for name, contents in (("task_history.json", job.task_history), (".deletion-cleanup", marker)):
            path = staging / name
            path.unlink(missing_ok=True)
            with path.open("w", encoding="utf-8") as stream:
                _ = stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
        staging.rename(target)
    if source.exists():
        shutil.rmtree(source)
