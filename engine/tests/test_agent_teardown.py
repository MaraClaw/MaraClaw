import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pywhales.exceptions import DockerException

from app.records.agent import AgentRecord
from app.records.agent_deletion_cleanup import AgentDeletionCleanupRecord
from app.services import agent_deletion_cleanup, agent_stops


@pytest.mark.parametrize("failure", ["docker", "archive"])
async def test_teardown_isolates_container_and_archive_errors_per_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    # Given two durable jobs, with the first failing before cleanup can complete.
    first = AgentRecord(id=uuid4(), name="Broken", creator_id=uuid4())
    second = AgentRecord(id=uuid4(), name="Healthy", creator_id=uuid4())
    jobs = {
        agent.id: AgentDeletionCleanupRecord(
            agent_id=agent.id,
            name=agent.name,
            creator_id=agent.creator_id,
            tenant_id=agent.tenant_id,
            container_id=agent.container_id,
            workspace_path=tmp_path / str(agent.id),
            archive_path=tmp_path / "archive" / str(agent.id),
            task_history=history,
        )
        for agent, history in ((first, "first"), (second, "second"))
    }
    for job in jobs.values():
        job.workspace_path.mkdir()
        (job.workspace_path / "SOUL.md").write_text(job.name)
    pending = dict(jobs)
    attempts = dict.fromkeys(jobs, 0)
    errors = dict[UUID, str | None].fromkeys(jobs)
    calls: list[tuple[str, UUID]] = []

    async def begin_attempt(agent_id: UUID) -> None:
        attempts[agent_id] += 1
        errors[agent_id] = None

    async def progress(agent_id: UUID, *, completed: bool = False, error: str | None = None) -> None:
        errors[agent_id] = error
        if completed:
            del pending[agent_id]

    async def remove(agent: AgentRecord) -> bool:
        calls.append(("remove", agent.id))
        if agent.id == first.id and attempts[agent.id] == 1 and failure == "docker":
            raise DockerException(["docker", "rm"], 1, b"", b"unavailable")
        return True

    publish = agent_deletion_cleanup.publish_deletion_archive

    def archive(job: AgentDeletionCleanupRecord) -> None:
        calls.append(("archive", job.agent_id))
        if job.agent_id == first.id and attempts[job.agent_id] == 1 and failure == "archive":
            raise OSError("archive unavailable")
        publish(job)

    monkeypatch.setattr(agent_deletion_cleanup.cleanup, "get_pending", AsyncMock(side_effect=pending.get))
    monkeypatch.setattr(agent_deletion_cleanup.cleanup, "pending", AsyncMock(side_effect=lambda: list(pending)))
    monkeypatch.setattr(agent_deletion_cleanup.cleanup, "begin_attempt", begin_attempt)
    monkeypatch.setattr(agent_deletion_cleanup.cleanup, "progress", progress)
    monkeypatch.setattr(agent_deletion_cleanup, "publish_deletion_archive", archive)
    monkeypatch.setattr(
        agent_deletion_cleanup,
        "agent_manager",
        SimpleNamespace(
            docker_client=SimpleNamespace(container=SimpleNamespace(list=lambda **_kwargs: [])),
            remove_container=remove,
        ),
    )
    # When the first job fails, the next job still reaches a complete filesystem archive.
    await asyncio.wait_for(
        agent_stops.teardown_deleted_agents(
            [agent_stops.AgentTeardown(first, "first"), agent_stops.AgentTeardown(second, "second")]
        ),
        5,
    )
    first_archive = [("archive", first.id)] if failure == "archive" else []
    assert calls == [("remove", first.id), *first_archive, ("remove", second.id), ("archive", second.id)]
    assert pending == {first.id: jobs[first.id]}
    assert attempts == {first.id: 1, second.id: 1}
    first_error = errors[first.id]
    assert first_error is not None
    assert first_error.startswith("DockerException:" if failure == "docker" else "OSError:")
    assert errors[second.id] is None
    assert (jobs[first.id].workspace_path / "SOUL.md").read_text() == "Broken"
    assert not jobs[first.id].archive_path.exists()
    assert not jobs[second.id].workspace_path.exists()
    assert (jobs[second.id].archive_path / "task_history.json").read_text() == "second"
    # Then worker replay retries the retained failure without reprocessing the completed job.
    for _ in range(2):
        await asyncio.wait_for(agent_deletion_cleanup.reconcile_deletion_cleanup(), 5)
    assert calls == [
        ("remove", first.id),
        *first_archive,
        ("remove", second.id),
        ("archive", second.id),
        ("remove", first.id),
        ("archive", first.id),
    ]
    assert pending == {}
    assert attempts == {first.id: 2, second.id: 1}
    assert errors == {first.id: None, second.id: None}
    assert not jobs[first.id].workspace_path.exists()
    assert (jobs[first.id].archive_path / "task_history.json").read_text() == "first"
    assert (jobs[first.id].archive_path / "SOUL.md").read_text() == "Broken"
