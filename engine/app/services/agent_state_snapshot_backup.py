"""One state backup: the scratch directory it writes into and the one-off container that takes it."""

from pathlib import Path
from shutil import rmtree
from typing import Final

from pywhales import DockerClient

from app.config import get_settings
from app.records.agent import AgentRecord
from app.services.agent_state_snapshot import (
    ARCHIVE_NAME,
    CONTAINER_TMP,
    GUEST_STATE_DIR,
    new_work_dir,
    run_one_off,
)

CREATE_ROLE: Final = "create"
_BACKUP_SCRIPT: Final = (
    "exec timeout -s KILL 120 openclaw backup create --no-include-workspace --verify --json "
    f"--output /out/{ARCHIVE_NAME} 2>/out/backup.log"
)
_GUEST_ENVS: Final = {
    "HOME": "/home/node",
    "OPENCLAW_HOME": "/home/node",
    "OPENCLAW_STATE_DIR": GUEST_STATE_DIR,
    "OPENCLAW_CONFIG_PATH": f"{GUEST_STATE_DIR}/openclaw.json",
    "TMPDIR": CONTAINER_TMP,
}


def new_out_dir(agents: Path) -> Path:
    out = new_work_dir(agents, CREATE_ROLE) / "out"
    try:
        out.mkdir()
    except OSError:
        rmtree(out.parent, ignore_errors=True)
        raise
    return out


def run_backup(client: DockerClient, agent: AgentRecord, agent_dir: Path, out: Path) -> str:
    """The backup command's stdout; `agent_dir` is mounted read-write and `out` receives the archive."""
    return run_one_off(
        client,
        agent.openclaw_image_id or get_settings().MARACLAW_OPENCLAW_IMAGE,
        _BACKUP_SCRIPT,
        name=f"maraclaw-state-snapshot-{agent.id}",
        role=CREATE_ROLE,
        agent_id=agent.id,
        mounts={agent_dir: GUEST_STATE_DIR, out: "/out"},
        envs=_GUEST_ENVS,
    )
