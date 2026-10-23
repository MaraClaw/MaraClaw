"""OpenClaw cutover executor: success, rollback paths, interrupted recovery and desired-state CAS."""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from typing import override

import pytest
from python_on_whales.exceptions import DockerException

from app.records.agent import AgentRecord
from app.records.openclaw_update import OpenClawUpdateRunRecord, RunJournal
from app.services import openclaw_update_executor as executor_module
from app.services.agent_manager import guest_container_name
from app.services.guest_identity import GuestOwnershipError
from app.services.openclaw_update_docker import ContainerInfo
from app.services.openclaw_update_executor import READY_PASSES, UpgradeExecutor
from app.services.openclaw_update_snapshot import commit_snapshot, restore_snapshot, snapshot_path
from openclaw_update_fakes import NEW_IMAGE, OLD_IMAGE, FakeContainer, FakeDocker, FakeManager, FakeStore


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.agent = AgentRecord(
            id=uuid.uuid4(),
            name="Ada",
            creator_id=uuid.uuid4(),
            status="running",
            container_id="old-id",
            container_port=18800,
        )
        self.run = OpenClawUpdateRunRecord(
            id=uuid.uuid4(), notice_id=uuid.uuid4(), agent_id=self.agent.id, state="pending"
        )
        self.docker = FakeDocker()
        self.docker.containers["old-id"] = FakeContainer(
            "old-id", guest_container_name(self.agent.id), OLD_IMAGE, agent_id=str(self.agent.id)
        )
        self.manager = FakeManager(self.docker, tmp_path)
        self.store = FakeStore({self.agent.id: self.agent}, {self.run.id: self.run})
        self.agent_dir = self.manager._agent_dir(self.agent.id)
        (self.agent_dir / "workspace").mkdir(parents=True)
        (self.agent_dir / "workspace" / "memory.md").write_text("before", encoding="utf-8")
        self.executor = UpgradeExecutor(
            self.store,
            self.docker,
            self.manager,
            ready_timeout=0.05,
            ready_interval=0,
        )

    @property
    def stored_run(self) -> OpenClawUpdateRunRecord:
        return self.store.runs[self.run.id]

    @property
    def stored_agent(self) -> AgentRecord:
        return self.store.agents[self.agent.id]

    def running_names(self) -> dict[str, str]:
        return {c.name: c.id for c in self.docker.containers.values()}


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


async def test_success_publishes_new_container_pins_target_and_cleans_old_artifacts(h: Harness):
    # When the run executes against a healthy agent.
    await h.executor.process(h.run.id)
    # Then the DB points at the new writer, pinned to the immutable pulled image id, in one atomic publication.
    assert h.stored_run.state == "succeeded"
    assert h.stored_agent.container_id != "old-id"
    assert h.stored_agent.openclaw_image_id == NEW_IMAGE
    assert h.stored_agent.container_port == 18900
    # And the replacement ran the immutable id without rehydrating; the old parked container and snapshot are gone.
    assert h.manager.starts == [(NEW_IMAGE, False)]
    assert "old-id" not in h.docker.containers
    assert not snapshot_path(h.agent_dir, h.run.id).exists()
    assert (h.agent_dir / "workspace" / "written-by-new-writer.txt").is_file()
    assert h.store.requeued == [h.agent.id]


async def test_cleanup_ownership_refusal_preserves_published_success_and_continues_cleanup(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the parked guest's ownership is refused when cleanup inspects it after publication.
    foreign_owner = str(uuid.uuid4())
    inspect = FakeDocker.inspect

    def inspect_after_publication(docker: FakeDocker, reference: str) -> ContainerInfo | None:
        if reference == "old-id" and h.stored_run.state == "succeeded":
            docker.containers[reference].agent_id = foreign_owner
        return inspect(docker, reference)

    warnings: list[tuple[uuid.UUID, GuestOwnershipError]] = []

    def record_warning(_message: str, run_id: uuid.UUID, error: GuestOwnershipError) -> None:
        assert h.stored_run.state == "succeeded"
        warnings.append((run_id, error))

    monkeypatch.setattr(FakeDocker, "inspect", inspect_after_publication)
    monkeypatch.setattr(executor_module.logger, "warning", record_warning)
    # When the healthy replacement is published and post-success cleanup runs.
    await h.executor.process(h.run.id)
    # Then the new writer remains published, the foreign guest is untouched, and snapshot cleanup still completes.
    assert h.stored_run.state == "succeeded"
    assert h.stored_agent.status == "running"
    assert h.stored_agent.openclaw_image_id == NEW_IMAGE
    new_id = h.stored_agent.container_id
    assert new_id is not None
    assert new_id != "old-id"
    assert h.docker.containers[new_id].running
    parked = f"{guest_container_name(h.agent.id)}-prev-{str(h.run.id)[:8]}"
    assert h.docker.calls == [("restart", "old-id=no"), ("stop", "old-id"), ("rename", f"old-id->{parked}")]
    assert h.docker.containers["old-id"].agent_id == foreign_owner
    assert not snapshot_path(h.agent_dir, h.run.id).exists()
    assert len(warnings) == 1
    run_id, refusal = warnings[0]
    assert run_id == h.run.id
    assert (refusal.reference, refusal.actual_agent_id) == ("old-id", foreign_owner)


async def test_old_container_is_parked_with_no_restart_before_the_snapshot_and_new_start(h: Harness):
    await h.executor.process(h.run.id)
    calls = h.docker.calls[:3]
    parked = f"{guest_container_name(h.agent.id)}-prev-{str(h.run.id)[:8]}"
    assert calls == [("restart", "old-id=no"), ("stop", "old-id"), ("rename", f"old-id->{parked}")]


async def test_pull_failure_fails_the_run_and_leaves_the_old_guest_untouched(h: Harness):
    h.docker.pull_error = True
    await h.executor.process(h.run.id)
    assert (h.stored_run.state, h.stored_run.failure_code) == ("failed", "pull_failed")
    assert h.docker.calls == []
    assert h.docker.containers["old-id"].running
    assert h.stored_agent.container_id == "old-id"
    assert h.manager.starts == []


@pytest.mark.parametrize("status", ["stopped", "error"])
async def test_stopped_agent_fails_not_running_without_mutation(h: Harness, status: str):
    h.store.agents[h.agent.id] = replace(h.agent, status=status)
    await h.executor.process(h.run.id)
    assert (h.stored_run.state, h.stored_run.failure_code) == ("failed", "not_running")
    assert h.docker.calls == []


async def test_new_container_start_failure_rolls_back_state_and_restarts_the_original(h: Harness):
    # Given the new writer dirties the shared state before it fails.
    h.manager.fail_start = True
    await h.executor.process(h.run.id)
    # Then the run failed, the writer's changes were discarded and the original is canonical, running and pinned.
    assert (h.stored_run.state, h.stored_run.failure_code) == ("failed", "start_failed")
    assert not (h.agent_dir / "workspace" / "written-by-new-writer.txt").exists()
    assert (h.agent_dir / "workspace" / "memory.md").read_text(encoding="utf-8") == "before"
    original = h.docker.containers["old-id"]
    assert original.name == guest_container_name(h.agent.id)
    assert original.running
    assert original.restart == "unless-stopped"
    assert h.stored_agent.openclaw_image_id == OLD_IMAGE
    assert h.stored_agent.container_id == "old-id"
    assert h.store.requeued == [h.agent.id, h.agent.id]


async def test_readiness_failure_removes_new_writer_before_restoring_state(h: Harness):
    h.docker.ready_results = {}
    created: list[str] = []

    def never_ready(reference: str) -> None:
        if reference.startswith("new-"):
            created.append(reference)
            h.docker.ready_results[reference] = [False] * 100

    h.docker.on_ready = never_ready
    await h.executor.process(h.run.id)
    assert (h.stored_run.state, h.stored_run.failure_code) == ("failed", "not_ready")
    assert created, "readiness was probed on the new writer"
    removal = h.docker.calls.index(("remove", created[0]))
    assert removal < h.docker.calls.index(("start", "old-id"))
    assert set(h.running_names()) == {guest_container_name(h.agent.id)}
    assert h.running_names()[guest_container_name(h.agent.id)] == "old-id"
    assert (h.agent_dir / "workspace" / "memory.md").read_text(encoding="utf-8") == "before"


async def test_readiness_needs_consecutive_passes_not_one_lucky_probe(h: Harness):
    # Given probes that pass, fail, then pass twice.
    def script_probes(reference: str) -> None:
        _ = h.docker.ready_results.setdefault(reference, [True, False, True, True])

    h.docker.on_ready = script_probes
    await h.executor.process(h.run.id)
    assert READY_PASSES == 2
    assert h.stored_run.state == "succeeded"


async def test_run_in_cutover_rolls_back_instead_of_rolling_forward(h: Harness):
    # Given a worker died after the new writer started: parked original, committed snapshot, dirty state.
    canonical = guest_container_name(h.agent.id)
    parked = f"{canonical}-prev-{str(h.run.id)[:8]}"
    h.docker.containers["old-id"].name = parked
    h.docker.containers["old-id"].running = False
    h.docker.containers["old-id"].restart = "no"
    h.docker.containers["new-1"] = FakeContainer("new-1", canonical, NEW_IMAGE, agent_id=str(h.agent.id))
    _ = commit_snapshot(h.agent_dir, h.run.id)
    (h.agent_dir / "workspace" / "memory.md").write_text("after the new writer", encoding="utf-8")
    journal = RunJournal(
        target_image_id=NEW_IMAGE,
        old_container_id="old-id",
        old_image_id=OLD_IMAGE,
        canonical_name=canonical,
        parked_name=parked,
    )
    h.store.runs[h.run.id] = replace(h.run, state="cutover", journal=journal)
    # When another worker takes the run.
    await h.executor.process(h.run.id)
    # Then it rolled back (and never started anything new).
    assert (h.stored_run.state, h.stored_run.failure_code) == ("failed", "interrupted")
    assert "new-1" not in h.docker.containers
    assert h.docker.containers["old-id"].name == canonical
    assert h.docker.containers["old-id"].running
    assert (h.agent_dir / "workspace" / "memory.md").read_text(encoding="utf-8") == "before"
    assert h.manager.starts == []
    assert h.stored_agent.openclaw_image_id == OLD_IMAGE
    assert h.store.requeued == [h.agent.id]


async def test_interrupted_cutover_stops_original_before_restore_and_requeue(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a worker died just after journaling cutover, before stopping the original or taking a snapshot.
    canonical = guest_container_name(h.agent.id)
    journal = RunJournal(
        target_image_id=NEW_IMAGE,
        old_container_id="old-id",
        old_image_id=OLD_IMAGE,
        canonical_name=canonical,
        parked_name=f"{canonical}-prev-{str(h.run.id)[:8]}",
    )
    h.store.runs[h.run.id] = replace(h.run, state="cutover", journal=journal)
    observed: list[tuple[str, bool]] = []
    requeue = FakeStore.requeue_inflight

    def observe_restore(agent_dir: Path, run_id: uuid.UUID) -> bool:
        observed.append(("restore", h.docker.containers["old-id"].running))
        return restore_snapshot(agent_dir, run_id)

    async def observe_requeue(store: FakeStore, agent_id: uuid.UUID) -> None:
        observed.append(("requeue", h.docker.containers["old-id"].running))
        await requeue(store, agent_id)

    monkeypatch.setattr(executor_module, "restore_snapshot", observe_restore)
    monkeypatch.setattr(FakeStore, "requeue_inflight", observe_requeue)
    # When another worker recovers the interrupted run.
    await h.executor.process(h.run.id)
    # Then restore and requeue run in order with the original stopped, before it resumes service.
    assert observed == [("restore", False), ("requeue", False)]
    assert h.store.requeued == [h.agent.id]
    assert h.docker.containers["old-id"].running
    assert (h.stored_run.state, h.stored_run.failure_code) == ("failed", "interrupted")


async def test_crash_between_the_rename_steps_is_finished_by_recovery(h: Harness):
    # Given the restore's first rename (agent dir -> discard) finished and nothing else did.
    canonical = guest_container_name(h.agent.id)
    parked = f"{canonical}-prev-{str(h.run.id)[:8]}"
    h.docker.containers["old-id"].name = parked
    h.docker.containers["old-id"].running = False
    _ = commit_snapshot(h.agent_dir, h.run.id)
    (h.agent_dir / "workspace" / "memory.md").write_text("new writer", encoding="utf-8")
    discard = h.agent_dir.with_name(f"{h.agent_dir.name}.rollback-{str(h.run.id)[:8]}")
    h.agent_dir.rename(discard)
    journal = RunJournal(
        target_image_id=NEW_IMAGE,
        old_container_id="old-id",
        old_image_id=OLD_IMAGE,
        canonical_name=canonical,
        parked_name=parked,
    )
    h.store.runs[h.run.id] = replace(h.run, state="cutover", journal=journal)
    await h.executor.process(h.run.id)
    assert h.stored_run.failure_code == "interrupted"
    assert (h.agent_dir / "workspace" / "memory.md").read_text(encoding="utf-8") == "before"
    assert h.docker.containers["old-id"].running


async def test_admin_stop_during_cutover_never_resurrects_the_agent(h: Harness):
    # Given a disable lands while the new writer is becoming ready.
    def disable(reference: str) -> None:
        if reference.startswith("new-"):
            h.store.agents[h.agent.id] = replace(h.store.agents[h.agent.id], status="stopped")

    h.docker.on_ready = disable
    await h.executor.process(h.run.id)
    # Then publication's CAS refused, the writer is gone, and the original stays stopped under its canonical name.
    assert (h.stored_run.state, h.stored_run.failure_code) == ("failed", "publish_conflict")
    assert h.stored_agent.status == "stopped"
    assert h.stored_agent.container_id == "old-id"
    original = h.docker.containers["old-id"]
    assert original.name == guest_container_name(h.agent.id)
    assert not original.running
    assert len(h.docker.containers) == 1


async def test_deleted_agent_during_cutover_removes_every_container_and_resurrects_nothing(h: Harness):
    def delete(reference: str) -> None:
        if reference.startswith("new-"):
            h.store.agents.pop(h.agent.id, None)

    h.docker.on_ready = delete
    await h.executor.process(h.run.id)
    assert h.docker.containers == {}
    assert h.agent.id not in h.store.agents
    assert h.stored_run.state == "failed"


async def test_rollback_failure_is_visible_and_keeps_artifacts(h: Harness):
    # Given the new start fails and the parked original disappears before rollback can restart it.
    h.manager.fail_start = True

    def lose_original() -> None:
        del h.docker.containers["old-id"]

    h.manager.on_start_failure = lose_original
    await h.executor.process(h.run.id)
    assert (h.stored_run.state, h.stored_run.failure_code) == ("failed", "rollback_failed")
    assert h.stored_agent.status == "error"
    assert h.stored_agent.openclaw_image_id == OLD_IMAGE
    assert list(h.agent_dir.parent.glob("*.rollback-*")), "discarded writer state is preserved for inspection"


async def test_a_second_worker_finding_a_terminal_run_does_nothing(h: Harness):
    await h.executor.process(h.run.id)
    calls_after_first = list(h.docker.calls)
    await h.executor.process(h.run.id)
    assert h.docker.calls == calls_after_first


class _PullFailsOnceOwnerIsInCutover(FakeDocker):
    """Worker B's docker: its pull blocks (in its thread) until worker A holds the run in cutover, then fails."""

    owner_in_cutover: threading.Event

    @override
    def pull(self, reference: str) -> str:
        assert self.owner_in_cutover.wait(5)
        raise DockerException(["docker", "pull", reference], 1)


async def test_second_workers_failed_pull_cannot_terminate_a_run_the_first_worker_owns(h: Harness):
    # Given worker B read the run as pending and is pulling when worker A takes it into cutover.
    owner_in_cutover, release = threading.Event(), asyncio.Event()
    at_publication_gate = asyncio.Event()

    async def hold_before_publication() -> None:
        owner_in_cutover.set()
        at_publication_gate.set()
        await release.wait()

    failing = _PullFailsOnceOwnerIsInCutover(containers=h.docker.containers)
    failing.owner_in_cutover = owner_in_cutover
    second = asyncio.create_task(UpgradeExecutor(h.store, failing, h.manager).process(h.run.id))
    h.manager.after_start = hold_before_publication
    first = asyncio.create_task(h.executor.process(h.run.id))
    await at_publication_gate.wait()
    # When B's pull fails while A owns the run.
    await second
    # Then B did not fail A's active cutover, so A still publishes and the gate never opened early.
    assert h.stored_run.state == "cutover"
    release.set()
    await first
    assert h.stored_run.state == "succeeded"
    assert h.stored_agent.openclaw_image_id == NEW_IMAGE


async def test_failed_pull_still_terminates_a_run_nobody_has_started(h: Harness):
    h.docker.pull_error = True
    await h.executor.process(h.run.id)
    assert (h.stored_run.state, h.stored_run.failure_code) == ("failed", "pull_failed")


@pytest.mark.parametrize("foreign_ref", ["new-id", "canonical", "old-id"])
async def test_rollback_preflights_all_labels_before_removing_any_writer(h: Harness, foreign_ref: str) -> None:
    # Given recovery has an owned writer followed by a foreign canonical holder (or a foreign journal ID).
    canonical = f"maraclaw-agent-{h.agent.id}"
    h.docker.containers["old-id"].name = f"{canonical}-prev"
    h.docker.containers["old-id"].agent_id = str(uuid.uuid4() if foreign_ref == "old-id" else h.agent.id)
    for ref, name in (("new-id", "new-writer"), ("canonical", canonical)):
        h.docker.containers[ref] = FakeContainer(
            ref, name, NEW_IMAGE, agent_id=str(uuid.uuid4() if ref == foreign_ref else h.agent.id)
        )
    journal = RunJournal(old_container_id="old-id", new_container_id="new-id", canonical_name=canonical)
    run = replace(h.run, state="cutover", journal=journal)
    # When recovery tries to restore the original.
    with pytest.raises(GuestOwnershipError) as error:
        await h.executor._restore_original(run, h.agent_dir, journal)
    # Then every reference was checked before any stop, remove, rename or filesystem restore.
    assert error.value.agent_id == h.agent.id
    assert h.docker.calls == []
    assert (h.agent_dir / "workspace" / "memory.md").read_text(encoding="utf-8") == "before"


async def test_recovery_keeps_legacy_journal_names_compatible(h: Harness) -> None:
    # Given an interrupted upgrade journal produced by the shipped short-name implementation.
    canonical = f"maraclaw-agent-{str(h.agent.id)[:8]}"
    original = h.docker.containers["old-id"]
    original.name = f"{canonical}-prev"
    original.agent_id = str(h.agent.id)
    h.docker.containers["new-id"] = FakeContainer("new-id", canonical, NEW_IMAGE, agent_id=str(h.agent.id))
    journal = RunJournal(old_container_id="old-id", canonical_name=canonical)
    h.store.runs[h.run.id] = replace(h.run, state="cutover", journal=journal)
    # When the interrupted upgrade is recovered.
    await h.executor.process(h.run.id)
    # Then the labeled legacy original is restored without rewriting the persisted journal name.
    assert h.stored_run.failure_code == "interrupted"
    assert original.name == canonical
    assert set(h.docker.containers) == {"old-id"}


@pytest.mark.parametrize("foreign_ref", ["old-id", "canonical"])
async def test_cutover_rejects_foreign_refs_before_journaling_or_stopping(h: Harness, foreign_ref: str) -> None:
    # Given a foreign persisted guest or full-name holder before the cutover starts.
    owner = str(uuid.uuid4())
    if foreign_ref == "old-id":
        h.docker.containers["old-id"].agent_id = owner
    else:
        h.docker.containers["old-id"].name = "legacy-name"
        h.docker.containers["foreign"] = FakeContainer(
            "foreign", guest_container_name(h.agent.id), NEW_IMAGE, agent_id=owner
        )
    # When the worker processes the pending upgrade under its real exclusion lock.
    with pytest.raises(GuestOwnershipError):
        await h.executor.process(h.run.id)
    # Then neither Docker nor the recovery journal was mutated.
    assert h.docker.calls == []
    assert h.stored_run.state == "pending"
    assert h.stored_run.journal == {}
