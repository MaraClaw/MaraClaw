from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.api import agents as agents_api, background_tasks
from app.core.domain.users import UserRole
from app.db.session import connection_ctx
from app.services import agent_deletion_archive, agent_deletion_cleanup, agent_stops, tenant_provisioning
from app.services.agent_manager import agent_manager
from app.services.agent_setup_lock import setup_workspace_lock
from app.services.guest_identity import GuestOwnershipError
from deletion_content_pg import seed_deletion_content

if TYPE_CHECKING:
    from tenant_deletion_pg_fixtures import DeletionPostgres

pytest_plugins = ("tenant_deletion_pg_fixtures",)


def drop_accelerator(coroutine: Coroutine[object, object, None], *, description: str) -> None:
    coroutine.close()


@pytest.mark.parametrize("failure", ["rejected", "unavailable", "foreign"])
async def test_rejected_guest_removal_keeps_workspace_for_retry(deletion_pg: DeletionPostgres, monkeypatch, failure):
    # Given a guest that cannot be removed and a workspace with frozen task history.
    await seed_deletion_content(deletion_pg)
    remove = agent_manager.remove_container
    docker = agent_manager.docker_client
    if failure == "unavailable":
        monkeypatch.setattr(agent_manager, "docker_client", None)
    else:
        error = GuestOwnershipError(deletion_pg.agent_id, "guest", str(uuid4())) if failure == "foreign" else None
        monkeypatch.setattr(agent_manager, "remove_container", AsyncMock(return_value=False, side_effect=error))
    # When the platform admin deletes the agent.
    await agents_api.delete_agent(deletion_pg.agent_id, replace(deletion_pg.actor, role=UserRole.PLATFORM_ADMIN))
    await asyncio.gather(*tuple(background_tasks._tasks))
    # Then neither the workspace nor the history can be archived while the writer remains.
    assert (deletion_pg.runtime_root / str(deletion_pg.agent_id) / "SOUL.md").exists()
    assert not (deletion_pg.runtime_root / "archive").exists()
    row = (await deletion_pg.rows("SELECT phase, attempts, last_error FROM agent_deletion_cleanup"))[0]
    assert row["phase"] == "removing"
    assert row["attempts"] == 1
    assert row["last_error"]
    monkeypatch.setattr(agent_manager, "remove_container", remove)
    monkeypatch.setattr(agent_manager, "docker_client", docker)
    await agent_deletion_cleanup.reconcile_deletion_cleanup()
    assert await deletion_pg.rows("SELECT phase, attempts, last_error FROM agent_deletion_cleanup") == [
        {"phase": "completed", "attempts": 2, "last_error": None}
    ]
    assert not (deletion_pg.runtime_root / str(deletion_pg.agent_id)).exists()


@pytest.mark.parametrize("tenant", [False, True])
async def test_worker_replays_frozen_history_without_postcommit_callback(
    deletion_pg: DeletionPostgres, monkeypatch, tenant
):
    # Given deletion with no surviving process-local callback.
    await seed_deletion_content(deletion_pg)
    monkeypatch.setattr(agent_stops, "schedule_background_task", drop_accelerator)
    actor = replace(deletion_pg.actor, role=UserRole.PLATFORM_ADMIN)
    # When deletion commits and a later worker reconciliation runs.
    if tenant:
        await tenant_provisioning.delete_tenant_and_release_identities(deletion_pg.tenant_id)
    else:
        await agents_api.delete_agent(deletion_pg.agent_id, actor)
    assert deletion_pg.runtime_calls == []
    assert await deletion_pg.rows("SELECT id FROM tasks WHERE agent_id=%s", (deletion_pg.agent_id,)) == []
    frozen = (await deletion_pg.rows("SELECT task_history FROM agent_deletion_cleanup"))[0]["task_history"]
    assert isinstance(frozen, str)
    monkeypatch.setattr(agent_stops.agent_dao, "prepare_container_stops", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_stops, "sweep_orphan_containers", AsyncMock())
    await agent_stops.reconcile_stopped_containers(datetime.now(UTC))
    await agent_stops.reconcile_stopped_containers(datetime.now(UTC))
    # Then one complete archive survives even after all task/log/tenant rows are gone.
    archive = deletion_pg.runtime_root / "archive" / str(deletion_pg.agent_id)
    assert (archive / "task_history.json").read_text() == frozen
    assert json.loads(frozen)["tasks"][0]["logs"][0]["content"] == "Completed"
    assert (archive / "SOUL.md").exists()
    assert deletion_pg.runtime_calls == [("remove", deletion_pg.agent_id), ("archive", deletion_pg.agent_id)]
    assert await deletion_pg.rows("SELECT phase, attempts FROM agent_deletion_cleanup") == [
        {"phase": "completed", "attempts": 1}
    ]


@pytest.mark.parametrize("boundary", ["copy", "publish", "source_removal", "completion"])
async def test_interrupted_archive_replays_to_the_same_target(deletion_pg: DeletionPostgres, monkeypatch, boundary):
    # Given committed cleanup whose worker dies at a publication boundary.
    await seed_deletion_content(deletion_pg)
    monkeypatch.setattr(agent_stops, "schedule_background_task", drop_accelerator)
    await agents_api.delete_agent(deletion_pg.agent_id, replace(deletion_pg.actor, role=UserRole.PLATFORM_ADMIN))
    archive = deletion_pg.runtime_root / "archive" / str(deletion_pg.agent_id)
    source = deletion_pg.runtime_root / str(deletion_pg.agent_id)
    history = (await deletion_pg.rows("SELECT task_history FROM agent_deletion_cleanup"))[0]["task_history"]

    class ProcessDeath(BaseException):
        pass

    copytree = agent_deletion_archive.shutil.copytree
    rename = Path.rename
    rmtree = agent_deletion_archive.shutil.rmtree
    progress = agent_deletion_cleanup.cleanup.progress

    def copying(src, dst, *args, **kwargs):
        Path(dst).mkdir()
        (Path(dst) / "partial").write_text("unfinished")
        raise ProcessDeath

    def publishing(path, target):
        raise ProcessDeath

    def removing(path: Path) -> None:
        if path == source:
            (source / "SOUL.md").unlink(missing_ok=True)
            raise ProcessDeath
        rmtree(path)

    async def completing(agent_id, *, completed=False, error=None):
        if completed:
            raise ProcessDeath
        await progress(agent_id, completed=completed, error=error)

    match boundary:
        case "copy":
            monkeypatch.setattr(agent_deletion_archive.shutil, "copytree", copying)
        case "publish":
            monkeypatch.setattr(Path, "rename", publishing)
        case "source_removal":
            monkeypatch.setattr(agent_deletion_archive.shutil, "rmtree", removing)
        case "completion":
            monkeypatch.setattr(agent_deletion_cleanup.cleanup, "progress", completing)
    # When the surviving durable row is replayed after process death.
    with pytest.raises(ProcessDeath):
        await agent_deletion_cleanup.complete_deletion_cleanup(deletion_pg.agent_id)
    assert await deletion_pg.rows("SELECT phase FROM agent_deletion_cleanup") == [{"phase": "archiving"}]
    monkeypatch.setattr(agent_deletion_archive.shutil, "copytree", copytree)
    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(agent_deletion_archive.shutil, "rmtree", rmtree)
    monkeypatch.setattr(agent_deletion_cleanup.cleanup, "progress", progress)
    await agent_deletion_cleanup.reconcile_deletion_cleanup()
    # Then publication is atomic, frozen and unique, and a partial source deletion loses nothing.
    assert (archive / "task_history.json").read_text() == history
    assert (archive / "SOUL.md").exists()
    assert not source.exists()
    assert list(archive.parent.iterdir()) == [archive]
    assert await deletion_pg.rows("SELECT phase, attempts FROM agent_deletion_cleanup") == [
        {"phase": "completed", "attempts": 2}
    ]


async def test_busy_workspace_defers_cleanup_until_next_replay(deletion_pg: DeletionPostgres, monkeypatch):
    # Given committed cleanup blocked by an existing lifecycle writer.
    await seed_deletion_content(deletion_pg)
    monkeypatch.setattr(agent_stops, "schedule_background_task", drop_accelerator)
    await agents_api.delete_agent(deletion_pg.agent_id, replace(deletion_pg.actor, role=UserRole.PLATFORM_ADMIN))
    # When a worker tries the busy workspace and later retries it.
    async with setup_workspace_lock(deletion_pg.runtime_root / str(deletion_pg.agent_id)):
        await agent_deletion_cleanup.reconcile_deletion_cleanup()
        assert deletion_pg.runtime_calls == []
    await agent_deletion_cleanup.reconcile_deletion_cleanup()
    # Then the lock refusal is recoverable without consuming an attempt.
    assert await deletion_pg.rows("SELECT phase, attempts FROM agent_deletion_cleanup") == [
        {"phase": "completed", "attempts": 1}
    ]


async def test_snapshot_freezes_once_inside_the_owning_transaction(deletion_pg: DeletionPostgres):
    # Given tasks whose history may change after the first deletion snapshot.
    await seed_deletion_content(deletion_pg)
    agent = await agents_api.agent_dao.get(deletion_pg.agent_id)
    assert agent is not None
    # When duplicate snapshot preparation joins the same transaction.
    async with connection_ctx() as db:
        first = await agent_stops.snapshot_agent_teardown([agent])
        await db.execute("UPDATE task_logs SET content='later'")
        second = await agent_stops.snapshot_agent_teardown([agent])
        assert await agent_deletion_cleanup.cleanup.get_pending(agent.id) is None
    # Then the unique job retains its original consistent frozen history.
    assert first[0].task_history == second[0].task_history
    assert json.loads(first[0].task_history or "{}")["tasks"][0]["logs"][0]["content"] == "Completed"
    assert len(await deletion_pg.rows("SELECT agent_id FROM agent_deletion_cleanup")) == 1


async def test_cancellation_drains_publication_under_the_workspace_lock(deletion_pg: DeletionPostgres, monkeypatch):
    # Given publication running in a non-abandoned filesystem thread.
    await seed_deletion_content(deletion_pg)
    monkeypatch.setattr(agent_stops, "schedule_background_task", drop_accelerator)
    await agents_api.delete_agent(deletion_pg.agent_id, replace(deletion_pg.actor, role=UserRole.PLATFORM_ADMIN))
    started, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    publish = agent_deletion_cleanup.publish_deletion_archive

    def publishing(job):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        publish(job)

    monkeypatch.setattr(agent_deletion_cleanup, "publish_deletion_archive", publishing)
    # When shutdown cancels the consumer during publication.
    task = asyncio.create_task(agent_deletion_cleanup.complete_deletion_cleanup(deletion_pg.agent_id))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Then cancellation propagates only after all effects have a durable completion record.
    assert await deletion_pg.rows("SELECT phase FROM agent_deletion_cleanup") == [{"phase": "completed"}]
    assert not (deletion_pg.runtime_root / str(deletion_pg.agent_id)).exists()
