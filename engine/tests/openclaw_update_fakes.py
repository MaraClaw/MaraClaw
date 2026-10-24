"""In-memory Docker, manager and store doubles for the OpenClaw upgrade executor."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from python_on_whales.exceptions import DockerException

from app.dao.openclaw_update_dao import SUCCEEDED_SWEEP_LIMIT
from app.records.agent import AgentRecord
from app.records.openclaw_update import OpenClawUpdateRunRecord, RunJournal
from app.services.agent_manager import guest_container_name
from app.services.openclaw_update_docker import ContainerInfo

NEW_IMAGE = "sha256:new-image"
OLD_IMAGE = "sha256:old-image"


@dataclass(slots=True)
class FakeContainer:
    id: str
    name: str
    image_id: str
    running: bool = True
    restart: str = "unless-stopped"
    agent_id: str | None = None


@dataclass(slots=True)
class FakeDocker:
    containers: dict[str, FakeContainer] = field(default_factory=dict[str, FakeContainer])
    pull_error: bool = False
    pulled: list[str] = field(default_factory=list[str])
    ready_results: dict[str, list[bool]] = field(default_factory=dict[str, list[bool]])
    on_ready: Callable[[str], None] | None = None
    calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def _find(self, ref: str) -> FakeContainer | None:
        for container in self.containers.values():
            if ref in (container.id, container.name):
                return container
        return None

    def pull(self, reference: str) -> str:
        self.pulled.append(reference)
        if self.pull_error:
            raise DockerException(["docker", "pull", reference], 1)
        return NEW_IMAGE

    def inspect(self, reference: str) -> ContainerInfo | None:
        found = self._find(reference)
        return ContainerInfo(found.id, found.name, found.image_id, found.agent_id) if found else None

    def stop(self, reference: str) -> None:
        self.calls.append(("stop", reference))
        found = self._find(reference)
        if found is not None:
            found.running = False

    def start(self, reference: str) -> None:
        self.calls.append(("start", reference))
        found = self._find(reference)
        if found is None:
            raise DockerException(["docker", "start", reference], 1)
        found.running = True

    def set_restart(self, reference: str, policy: str) -> None:
        self.calls.append(("restart", f"{reference}={policy}"))
        found = self._find(reference)
        if found is not None:
            found.restart = policy

    def rename(self, reference: str, name: str) -> None:
        self.calls.append(("rename", f"{reference}->{name}"))
        if any(c.name == name for c in self.containers.values()):
            raise DockerException(["docker", "rename"], 1)
        found = self._find(reference)
        if found is None:
            raise DockerException(["docker", "rename"], 1)
        found.name = name

    def remove(self, reference: str) -> None:
        self.calls.append(("remove", reference))
        found = self._find(reference)
        if found is not None:
            del self.containers[found.id]

    def ready(self, reference: str) -> bool:
        if self.on_ready is not None:
            self.on_ready(reference)
        found = self._find(reference)
        script = self.ready_results.get(reference, [])
        if script:
            return script.pop(0)
        return found is not None and found.running


class FakeManager:
    """Stands in for AgentManager: starts a named container and dirties the agent directory like a live writer."""

    def __init__(self, docker: FakeDocker, root: Path) -> None:
        self.docker: FakeDocker = docker
        self.docker_client: FakeDocker = docker
        self.root: Path = root
        self.starts: list[tuple[str, bool]] = []
        self.fail_start: bool = False
        self.on_start_failure: Callable[[], None] | None = None
        self.after_start: Callable[[], Awaitable[None]] | None = None

    def _agent_dir(self, agent_id: uuid.UUID) -> Path:
        return self.root / str(agent_id)

    async def start_container(
        self, agent: AgentRecord, *, image: str | None = None, rehydrate: bool = True
    ) -> str | None:
        self.starts.append((image or "", rehydrate))
        agent_dir = self._agent_dir(agent.id)
        (agent_dir / "workspace" / "written-by-new-writer.txt").write_text("new", encoding="utf-8")
        if self.fail_start:
            if self.on_start_failure is not None:
                self.on_start_failure()
            return None
        name = guest_container_name(agent.id)
        for stale in [c for c in self.docker.containers.values() if c.name == name]:
            del self.docker.containers[stale.id]
        container = FakeContainer(
            id=f"new-{uuid.uuid4().hex[:6]}", name=name, image_id=image or "", agent_id=str(agent.id)
        )
        self.docker.containers[container.id] = container
        agent.container_id = container.id
        agent.container_port = 18900
        if self.after_start is not None:
            await self.after_start()
        return container.id


@dataclass(slots=True)
class FakeStore:
    agents: dict[uuid.UUID, AgentRecord]
    runs: dict[uuid.UUID, OpenClawUpdateRunRecord]
    requeued: list[uuid.UUID] = field(default_factory=list[uuid.UUID])
    updated_at: dict[uuid.UUID, datetime] = field(default_factory=dict[uuid.UUID, datetime])

    async def list_active(self) -> list[OpenClawUpdateRunRecord]:
        return [run for run in self.runs.values() if run.active]

    async def list_recent_succeeded(self, since: datetime, after: uuid.UUID | None) -> list[OpenClawUpdateRunRecord]:
        return sorted(
            (
                run
                for run in self.runs.values()
                if run.state == "succeeded"
                and self.updated_at.get(run.id, run.created_at or since) >= since
                and (after is None or run.id > after)
            ),
            key=lambda run: run.id,
        )[:SUCCEEDED_SWEEP_LIMIT]

    async def active_for_agent(self, agent_id: uuid.UUID) -> OpenClawUpdateRunRecord | None:
        return next((run for run in self.runs.values() if run.agent_id == agent_id and run.active), None)

    async def get_run(self, run_id: uuid.UUID) -> OpenClawUpdateRunRecord | None:
        return self.runs.get(run_id)

    async def get_agent(self, agent_id: uuid.UUID) -> AgentRecord | None:
        agent = self.agents.get(agent_id)
        return replace(agent) if agent is not None else None

    async def begin_cutover(self, run_id: uuid.UUID, journal: RunJournal) -> bool:
        run = self.runs[run_id]
        if run.state != "pending":
            return False
        self.runs[run_id] = replace(run, state="cutover", journal=journal)
        return True

    async def set_journal(self, run_id: uuid.UUID, journal: RunJournal) -> None:
        self.runs[run_id] = replace(self.runs[run_id], journal=journal)

    async def finish_failed(self, run_id: uuid.UUID, code: str) -> None:
        run = self.runs[run_id]
        if run.active:
            self.runs[run_id] = replace(run, state="failed", failure_code=code)

    async def fail_pending(self, run_id: uuid.UUID, code: str) -> bool:
        run = self.runs[run_id]
        if run.state != "pending":
            return False
        self.runs[run_id] = replace(run, state="failed", failure_code=code)
        return True

    async def publish_success(
        self,
        run_id: uuid.UUID,
        agent_id: uuid.UUID,
        *,
        old_container_id: str,
        new_container_id: str,
        port: int | None,
        image_id: str,
    ) -> bool:
        agent, run = self.agents.get(agent_id), self.runs.get(run_id)
        if (
            agent is None
            or run is None
            or run.state != "cutover"
            or agent.container_id != old_container_id
            or agent.status not in ("running", "idle")
            or agent.is_expired
        ):
            return False
        self.agents[agent_id] = replace(
            agent, container_id=new_container_id, container_port=port, openclaw_image_id=image_id, status="running"
        )
        self.runs[run_id] = replace(run, state="succeeded")
        self.updated_at[run_id] = datetime.now(UTC)
        return True

    async def pin_image(self, agent_id: uuid.UUID, image_id: str) -> None:
        if agent_id in self.agents:
            self.agents[agent_id] = replace(self.agents[agent_id], openclaw_image_id=image_id)

    async def mark_rollback_failed(self, agent_id: uuid.UUID, image_id: str | None) -> None:
        if agent_id in self.agents:
            self.agents[agent_id] = replace(self.agents[agent_id], openclaw_image_id=image_id, status="error")

    async def requeue_inflight(self, agent_id: uuid.UUID) -> None:
        self.requeued.append(agent_id)
