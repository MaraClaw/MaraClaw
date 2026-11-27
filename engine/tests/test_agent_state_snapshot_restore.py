"""Restoring an agent's state snapshot onto a host without OpenClaw state, before its guest starts."""

import errno
import fcntl
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from pywhales import DockerClient
from pywhales.exceptions import DockerException

from app.config import get_settings
from app.records.agent import AgentRecord
from app.services import agent_manager as manager_module, agent_materialize, agent_state_snapshot_restore as restore
from app.services.agent_manager import AgentManager, _GuestModels
from app.services.agent_state_snapshot import (
    AGENT_DB,
    ROLE_LABEL,
    SnapshotName,
    agent_prefix,
    new_snapshot_name,
    tenant_token,
    work_root,
)
from app.services.agent_state_snapshot_restore import AgentStateRestoreError, place_restored_state
from app.services.docker_capacity import DockerBusyError
from state_snapshot_fakes import (
    IMAGE_VERSION,
    STATE_ASSET,
    MemoryStore,
    ScriptedDocker,
    openclaw_state,
    restore_by,
    write_archive,
    write_file,
)

GUEST_IMAGE = "openclaw:guest"
STATE_FILES = frozenset(
    {
        AGENT_DB,
        "state/openclaw.sqlite",
        "knowledge/registry.sqlite",
        "knowledge/stores/0b6b3c1e/state.sqlite",
        ".openclaw-gateway-token",
        "agents/main/agent/auth-profiles.json",
    }
)


@dataclass
class Host:
    tmp: Path
    root: Path
    agent: AgentRecord
    store: MemoryStore
    docker: ScriptedDocker

    @property
    def agent_dir(self) -> Path:
        return self.root / str(self.agent.id)

    def publish(
        self,
        day: int,
        *,
        tenant: str | None = None,
        token: bytes = b"gateway-token",
        runtime_version: str = IMAGE_VERSION,
        assets: Sequence[Mapping[str, object]] = (STATE_ASSET,),
        held: bool = False,
    ) -> SnapshotName:
        """Store the snapshot of an idle agent whose gateway token is `token`."""
        tenant = tenant_token(self.agent.tenant_id) if tenant is None else tenant
        name = new_snapshot_name("0" * 16, tenant, datetime(2026, 11, day, tzinfo=UTC))
        state = openclaw_state(self.tmp / "sources" / name.file_name)
        _ = write_file(state / ".openclaw-gateway-token", token, mode=0o600)
        archive = self.tmp / "archives" / name.file_name
        _ = write_archive(archive, state, runtime_version=runtime_version, assets=assets)
        self.store.objects[name.key(self.agent.id)] = archive.read_bytes()
        if held:
            self.store.objects[name.held_key(self.agent.id)] = b""
        return name

    async def restore(self) -> frozenset[str]:
        client = cast("DockerClient", self.docker)
        return await restore.restore_agent_state(client, self.agent, self.agent_dir, GUEST_IMAGE)

    def held(self) -> list[str]:
        return sorted(key for key in self.store.objects if key.endswith(".held"))

    def files(self) -> list[str]:
        if not self.agent_dir.exists():
            return []
        return sorted(
            path.relative_to(self.agent_dir).as_posix() for path in self.agent_dir.rglob("*") if path.is_file()
        )

    def leftovers(self) -> list[Path]:
        work = work_root(self.root) / "restore" / str(self.agent.id)
        return [work, *work.rglob("*")] if work.exists() else []


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    """A new host: an agents root without the agent's directory, an S3 store and the image's restore command."""
    root = tmp_path / "agents"
    root.mkdir()
    agent = AgentRecord(id=uuid4(), name="Restored", creator_id=uuid4(), tenant_id=uuid4())
    store = MemoryStore()
    monkeypatch.setattr(restore, "snapshot_store", lambda: store)
    return Host(tmp_path, root, agent, store, ScriptedDocker(restore_by()))


async def test_a_new_host_gets_the_newest_unheld_snapshot_of_the_agents_tenant(host: Host) -> None:
    # Given snapshots: an older one, the expected one, a newer held one, a newest one of a former tenant, and a note.
    _ = host.publish(1, token=b"older")
    _ = host.publish(2, token=b"expected")
    held = host.publish(3, token=b"held", held=True)
    _ = host.publish(4, tenant="none", token=b"former tenant")
    host.store.objects[f"{agent_prefix(host.agent.id)}README"] = b"note"
    # When the start restores.
    placed = await host.restore()
    # Then the expected snapshot's OpenClaw state is placed with its modes, and nothing the mirror owns.
    assert (host.agent_dir / ".openclaw-gateway-token").read_bytes() == b"expected"
    assert placed == STATE_FILES
    assert host.files() == sorted(STATE_FILES)
    assert stat.S_IMODE((host.agent_dir / ".openclaw-gateway-token").stat().st_mode) == 0o600
    # And the restore ran the guest's image offline in a removed one-off that saw only its work directory, now gone.
    [run] = host.docker.runs
    assert (run.image, run.options["name"]) == (GUEST_IMAGE, f"maraclaw-state-restore-{host.agent.id}")
    assert (run.options["networks"], run.options["remove"], run.options["labels"][ROLE_LABEL]) == (
        ["none"],
        True,
        "restore",
    )
    assert list(run.mounts) == ["/work"]
    assert run.mounts["/work"].is_relative_to(work_root(host.root) / "restore" / str(host.agent.id))
    assert run.options["envs"]["HOME"] == "/tmp"
    assert (host.leftovers(), host.held()) == ([], [held.held_key(host.agent.id)])


@pytest.mark.parametrize("database", [AGENT_DB, "state/openclaw.sqlite"])
async def test_a_host_with_either_openclaw_database_restores_nothing(host: Host, database: str) -> None:
    _ = host.publish(1)
    _ = write_file(host.agent_dir / database, b"")
    assert await host.restore() == frozenset()
    assert (host.store.listed, host.docker.runs, host.files()) == ([], [], [database])


async def test_without_a_usable_snapshot_the_start_goes_on_fresh(host: Host) -> None:
    _ = host.publish(1, held=True)
    _ = host.publish(2, tenant="none")
    assert await host.restore() == frozenset()
    assert (host.docker.runs, host.agent_dir.exists()) == ([], False)


def _unavailable(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "S3"}}, operation)


@pytest.mark.parametrize(
    "failure",
    ["listing", "download", "docker busy", "container failed", "restore killed", "snapshot busy", "placement"],
)
async def test_a_transient_failure_fails_the_start_with_nothing_placed_or_held(
    host: Host, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    # Given a snapshot and a failure a later start may not meet.
    _ = host.publish(1)

    def busy(_mounts: dict[str, Path]) -> str:
        raise DockerBusyError

    def failed(_mounts: dict[str, Path]) -> str:
        raise DockerException(["docker", "run"], 137, b"", b"")

    link = restore.link_guest_file
    linked: list[str] = []

    def link_then_fail(source: Path, root: Path, relative: str) -> bool:
        if len(linked) == 2:
            raise OSError(errno.EIO, "I/O error")
        linked.append(relative)
        return link(source, root, relative)

    match failure:
        case "listing":
            host.store.failures["list"] = _unavailable("ListObjectsV2")
        case "download":
            host.store.failures["read"] = _unavailable("GetObject")
        case "docker busy":
            host.docker.act = busy
        case "container failed":
            host.docker.act = failed
        case "restore killed":
            host.docker.act = restore_by(exit_code=137)
        case "snapshot busy":
            pass
        case _:
            monkeypatch.setattr(restore, "link_guest_file", link_then_fail)
    lock = host.root / "_setup_locks" / f"{host.agent.id}.snapshot.lock"
    lock.parent.mkdir()
    with lock.open("a+b") as held:
        if failure == "snapshot busy":
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # When the start restores.
        with pytest.raises(AgentStateRestoreError) as raised:
            _ = await host.restore()
    # Then the start fails as a preflight OSError with nothing placed, held or left in the work directory.
    assert isinstance(raised.value, OSError)
    assert (host.files(), host.held(), host.leftovers()) == ([], [], [])


@pytest.mark.parametrize("refusal", ["newer runtime", "archive rejected", "foreign asset", "two assets", "too large"])
async def test_a_snapshot_this_image_must_not_restore_is_held_and_the_start_goes_on_fresh(
    host: Host, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    # Given a snapshot this image cannot use.
    match refusal:
        case "newer runtime":
            name = host.publish(1, runtime_version="2026.12.1")
        case "archive rejected":
            name = host.publish(1)
            host.docker.act = restore_by(exit_code=1)
        case "foreign asset":
            name = host.publish(1, assets=[{**STATE_ASSET, "sourcePath": "/home/node/.ssh"}])
        case "two assets":
            name = host.publish(1, assets=[STATE_ASSET, {**STATE_ASSET, "kind": "workspace"}])
        case _:
            name = host.publish(1)
            monkeypatch.setattr(get_settings(), "AGENT_STATE_SNAPSHOT_MAX_BYTES", 64)
    # When the start restores, and again.
    assert await host.restore() == frozenset()
    runs = len(host.docker.runs)
    assert await host.restore() == frozenset()
    # Then the snapshot is held and kept, nothing was placed, and the second start did not try it again.
    assert host.held() == [name.held_key(host.agent.id)]
    assert name.key(host.agent.id) in host.store.objects
    assert (host.files(), host.leftovers(), len(host.docker.runs)) == ([], [], runs)


@pytest.mark.parametrize(
    ("snapshot", "image", "restored"),
    [
        ("2026.9.30", "2026.11.10", True),
        ("2026.11.10", "2026.11.10", True),
        ("2026.11.10-beta.2", "2026.11.10", True),
        ("2026.11.11", "2026.11.10", False),
        ("2027.1.1", "2026.11.10", False),
        ("nightly", "2026.11.10", False),
    ],
)
async def test_versions_compare_numerically(host: Host, snapshot: str, image: str, restored: bool) -> None:
    _ = host.publish(1, runtime_version=snapshot)
    host.docker.act = restore_by(image_version=image)
    assert (await host.restore() == STATE_FILES) is restored
    assert (host.held() == []) is restored


def test_placement_leaves_the_mirror_sidecars_existing_files_and_symlinked_paths_alone(tmp_path: Path) -> None:
    # Given a restored payload with sidecars, mirror and volatile files, engine bookkeeping and a symlink.
    payload = openclaw_state(tmp_path / "payload")
    for rel in (
        "state/openclaw.sqlite-wal",
        "knowledge/registry.sqlite-journal",
        f"{AGENT_DB}-shm",
        "soul.md",
        "skills/web/SKILL.md",
        "logs/gateway.log",
        "tmp/scratch",
        "plugin-skills/web/SKILL.md",
        ".maraclaw-mirror-generation",
        ".maraclaw-rpc-port",
    ):
        _ = write_file(payload / rel, b"skip")
    _ = write_file(payload / "openclaw.json", b"archived config")
    _ = write_file(payload / "control" / "services.sqlite", b"services")
    _ = write_file(payload / "client" / "credentials.sqlite", b"credentials", mode=0o600)
    (payload / "token-link").symlink_to(payload / ".openclaw-gateway-token")
    # And a host that already has a config and a symlinked directory pointing elsewhere.
    agent_dir, elsewhere = tmp_path / "agent", tmp_path / "elsewhere"
    elsewhere.mkdir()
    _ = write_file(agent_dir / "openclaw.json", b"host config")
    (agent_dir / "control").symlink_to(elsewhere)
    # When the payload is placed.
    placed = place_restored_state(payload, agent_dir)
    # Then only absent OpenClaw state was linked in, with its archived modes, and nothing went through the symlink.
    assert placed == STATE_FILES | {"client/credentials.sqlite"}
    assert (agent_dir / "openclaw.json").read_bytes() == b"host config"
    assert list(elsewhere.iterdir()) == []
    assert not (agent_dir / "token-link").exists()
    for rel in placed:
        assert (agent_dir / rel).stat().st_mode == (payload / rel).stat().st_mode
    assert sorted(path.relative_to(agent_dir).as_posix() for path in agent_dir.rglob("*") if path.is_file()) == sorted(
        placed | {"openclaw.json"}
    )


def test_placement_creates_missing_directories_with_their_archived_modes(tmp_path: Path) -> None:
    # Given a payload whose credential and Knowledge directories are private while the vault's Git tree is not.
    payload = openclaw_state(tmp_path / "payload")
    _ = write_file(payload / "client" / "credentials.sqlite", b"credentials", mode=0o600)
    _ = write_file(payload / "knowledge" / "stores" / "0b6b3c1e" / "vault" / ".git" / "HEAD", b"ref: refs/heads/main")
    for rel, mode in (
        ("client", 0o700),
        ("agents", 0o700),
        ("knowledge", 0o700),
        ("knowledge/stores/0b6b3c1e/vault/.git", 0o755),
    ):
        (payload / rel).chmod(mode)
    agent_dir = tmp_path / "agent"
    # When it is placed on a host that has none of those directories.
    placed = place_restored_state(payload, agent_dir)
    # Then each directory placement created has the payload's mode, so OpenClaw accepts the private ones.
    directories = {parent.as_posix() for rel in placed for parent in Path(rel).parents} - {"."}
    assert {"client", "agents/main/agent", "knowledge/stores/0b6b3c1e/vault/.git"} <= directories
    for rel in directories:
        assert stat.S_IMODE((agent_dir / rel).stat().st_mode) == stat.S_IMODE((payload / rel).stat().st_mode), rel


async def test_a_rehydrating_start_restores_with_the_guest_image_before_the_mirror_and_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a manager whose restore, mirror pull and storage materialization are recorded.
    monkeypatch.setattr(manager_module.settings, "STORAGE_LOCAL_ROOT", str(tmp_path))
    manager = AgentManager.__new__(AgentManager)
    agent = AgentRecord(id=uuid4(), name="Ordered", creator_id=uuid4())
    agent_dir = tmp_path / str(agent.id)
    calls: list[tuple[object, ...]] = []

    class MaterializedError(Exception):
        pass

    async def restoring(_client: DockerClient, _agent: AgentRecord, directory: Path, image: str) -> frozenset[str]:
        calls.append(("restore", directory, image))
        return frozenset({AGENT_DB})

    async def pulling(_agent_id: object, directory: Path) -> None:
        calls.append(("pull", directory))

    async def materializing(_agent_id: object, *, restored: frozenset[str] = frozenset()) -> Path:
        calls.append(("materialize", restored))
        raise MaterializedError

    monkeypatch.setattr(restore, "restore_agent_state", restoring)
    monkeypatch.setattr(agent_materialize, "pull_durable_state", pulling)
    monkeypatch.setattr(manager, "_materialize_agent_dir", materializing)
    # When the guest's state is applied for a start.
    with pytest.raises(MaterializedError):
        _ = await manager._apply_guest_state(
            agent,
            _GuestModels(primary=None, fallback=None),
            rehydrate=True,
            client=cast("DockerClient", object()),
            guest_image="openclaw:exact",
        )
    # Then the snapshot was restored with the guest's image first, and materialization skips what it placed.
    assert calls == [
        ("restore", agent_dir, "openclaw:exact"),
        ("pull", agent_dir),
        ("materialize", frozenset({AGENT_DB})),
    ]
