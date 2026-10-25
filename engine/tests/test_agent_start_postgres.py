from dataclasses import replace
from datetime import UTC, datetime
from importlib import import_module

import pytest

import test_openclaw_update_postgres as postgres_fixtures
from app.dao.agent_dao import agent_dao
from app.dao.agent_start_dao import START_OPERATIONS_DDL, agent_start_dao
from app.db.session import connection_ctx
from app.services import agent_start_reconciler as reconciler, guest_readiness
from app.services.agent_lifecycle import LifecycleSuccess, start_agent_guest
from app.services.agent_start_store import PostgresStartStore
from app.services.guest_identity import guest_container_name
from test_guest_start_safety import Docker
from test_openclaw_update_postgres import agent, member, tenant

db = postgres_fixtures.db


@pytest.fixture
async def start_db(db):
    await db.execute(START_OPERATIONS_DDL)
    return db


async def pending_start(db):
    company = await tenant(db)
    creator = await member(db, company)
    identifier = await agent(db, company, creator, status="stopped", container=None)
    pending = await PostgresStartStore().begin(identifier)
    assert pending is not None
    return pending


async def test_start_intent_survives_outer_request_rollback(start_db):
    # Given a request transaction whose lifetime is not the start operation's lifetime.
    store = PostgresStartStore()
    company = await tenant(start_db)
    creator = await member(start_db, company)
    identifier = await agent(start_db, company, creator, status="stopped", container=None)
    observed = []

    async def request():
        async with connection_ctx():
            replay = await store.begin(identifier)
            assert replay is not None
            observed.append(replay[0])
            assert await agent_start_dao.get(start_db, identifier) == replay[0]
            raise ValueError("request disconnected")

    # When that request rolls back after observing the committed operation.
    with pytest.raises(ValueError):
        await request()
    # Then another store instance can recover the exact same operation.
    recovered = await PostgresStartStore().recover(observed[0])
    assert recovered is not None
    assert recovered.id == identifier


@pytest.mark.parametrize("intent", ["stop", "tenant_disable", "creator_disable", "expiry", "transfer", "delete"])
async def test_later_intent_supersedes_pending_start(start_db, monkeypatch, intent):
    # Given a committed start that has not published its guest.
    operation, original = await pending_start(start_db)
    store = PostgresStartStore()

    async def bump(_identifier):
        return None

    monkeypatch.setattr(import_module("app.dao.agent_dao"), "bump_agent_acl_version", bump)
    # When a later control-plane intent commits through its owning DAO seam.
    if intent == "stop":
        await agent_dao.update(db_obj=original, obj_in={"status": "stopped"})
    elif intent == "tenant_disable":
        assert original.tenant_id is not None
        await agent_dao.disable_for_tenant(original.tenant_id)
    elif intent == "creator_disable":
        await agent_dao.disable_for_creator(original.creator_id)
    elif intent == "expiry":
        await start_db.execute(
            "UPDATE agents SET expires_at=now()-interval '1 second' WHERE id=%(id)s", {"id": original.id}
        )
        await agent_dao.prepare_container_stops(datetime.now(UTC))
    elif intent == "transfer":
        await agent_dao.update(db_obj=original, obj_in={"tenant_id": await tenant(start_db)})
    else:
        await start_db.execute("DELETE FROM agents WHERE id=%(id)s", {"id": original.id})
    # Then neither stale publication nor recovery can resurrect that intent.
    assert not await store.publish(operation, replace(original, status="running", container_id="new"))
    assert await store.recover(operation) is None
    current = await agent_start_dao.get(start_db, original.id)
    assert current is not None
    assert current.state == "cancelled"


@pytest.mark.parametrize("subject", ["user", "identity", "tenant"])
async def test_fresh_eligibility_blocks_disabled_subjects(start_db, subject):
    # Given a valid operation whose principal becomes inactive without a guest mutation.
    operation, original = await pending_start(start_db)
    # When the authoritative identity, membership or tenant is deactivated.
    if subject == "user":
        await start_db.execute("UPDATE users SET is_active=false WHERE id=%(id)s", {"id": original.creator_id})
    elif subject == "identity":
        await start_db.execute(
            "UPDATE identities SET is_active=false WHERE id=(SELECT identity_id FROM users WHERE id=%(id)s)",
            {"id": original.creator_id},
        )
    else:
        await start_db.execute("UPDATE tenants SET is_active=false WHERE id=%(id)s", {"id": original.tenant_id})
    # Then publication and recovery both fail closed against fresh database state.
    store = PostgresStartStore()
    assert not await store.publish(operation, replace(original, status="running", container_id="new"))
    assert await store.recover(operation) is None


@pytest.fixture
def guest_surface(start_db, tmp_path, monkeypatch):
    manager = reconciler.agent_manager
    docker = Docker()
    probes = []

    def execute(reference, command):
        assert "/readyz" in command[-1]
        probes.append(reference)
        return ""

    def resume(reference):
        docker.inspect(reference).state.status = "running"

    monkeypatch.setattr(docker, "execute", execute, raising=False)
    monkeypatch.setattr(docker, "start", resume, raising=False)
    monkeypatch.setattr(manager, "docker_client", docker)
    monkeypatch.setattr(manager, "_agent_dir", lambda identifier: tmp_path / str(identifier))
    monkeypatch.setattr(guest_readiness, "READY_INTERVAL_SECONDS", 0)
    return docker, probes


async def test_crash_candidate_recovery_publishes_only_after_two_probes(start_db, guest_surface):
    # Given a process died after Docker creation, before saving its canonical candidate ID.
    operation, original = await pending_start(start_db)
    docker, probes = guest_surface
    docker.add(replace(original, container_id="crash-candidate"))
    docker.guests["crash-candidate"].config.labels["maraclaw.start_operation"] = str(operation.operation_id)
    # When the outstanding-operation reconciler runs.
    await reconciler.reconcile_agent_start(operation)
    # Then the exact guest is adopted, not recreated, and only ready state is published.
    row = await start_db.fetchone("SELECT status,container_id FROM agents WHERE id=%(id)s", {"id": original.id})
    assert row == {"status": "running", "container_id": "crash-candidate"}
    assert probes == ["crash-candidate"] * 2
    assert docker.runs == docker.removed == []
    assert await PostgresStartStore().outstanding() == []


@pytest.mark.parametrize("committed", [False, True])
async def test_publication_failure_replays_without_destroying_healthy_guest(
    start_db, guest_surface, monkeypatch, committed
):
    # Given a ready canonical guest and a failed or ambiguously acknowledged database publication.
    operation, original = await pending_start(start_db)
    docker, probes = guest_surface
    docker.add(replace(original, container_id="ready-candidate"))
    store = reconciler.start_store
    publish = store.publish

    async def interrupted(current, candidate):
        if committed:
            assert await publish(current, candidate)
        raise OSError("commit acknowledgement lost")

    monkeypatch.setattr(store, "publish", interrupted)
    # When publication is interrupted and then the surviving pending operation is retried.
    result = await reconciler.execute_start(operation, original)
    monkeypatch.setattr(store, "publish", publish)
    if not committed:
        assert result is False
        await reconciler.reconcile_agent_start(operation)
    # Then ambiguity never destroys the healthy writer and authoritative state eventually wins.
    assert docker.guests["ready-candidate"].state.status == "running"
    assert docker.runs == docker.removed == []
    assert len(probes) == (2 if committed else 4)
    assert await store.outstanding() == []


async def test_deleted_agent_retains_cleanup_ownership_for_created_writer(start_db, guest_surface):
    # Given a labelled writer exists but its agent row has since been deleted.
    operation, original = await pending_start(start_db)
    docker, _probes = guest_surface
    guest = docker.add(replace(original, container_id="orphan-candidate"))
    guest.config.labels["maraclaw.start_operation"] = str(operation.operation_id)
    await start_db.execute("DELETE FROM agents WHERE id=%(id)s", {"id": original.id})
    # When recovery observes the missing row and the cleanup pass runs.
    await reconciler.reconcile_agent_start(operation)
    cancelled = await agent_start_dao.get(start_db, original.id)
    assert cancelled is not None
    await reconciler.reconcile_agent_start(cancelled)
    # Then the operation record survives deletion until its writer is quiesced.
    assert docker.inspect(guest_container_name(original.id)).state.status == "exited"
    assert await PostgresStartStore().outstanding() == []


async def test_real_start_surface_repeated_start_resumes_without_replacement(start_db, guest_surface):
    # Given an owned stopped guest behind the actual ordinary Start service.
    _operation, original = await pending_start(start_db)
    docker, probes = guest_surface
    docker.add(replace(original, container_id="same-id"), status="exited")
    # When the service receives consecutive ordinary Starts.
    first = await start_agent_guest(original.id)
    second = await start_agent_guest(original.id)
    # Then both responses identify the same ready guest and no replacement occurs.
    assert isinstance(first, LifecycleSuccess)
    assert isinstance(second, LifecycleSuccess)
    assert first.agent.container_id == second.agent.container_id == "same-id"
    assert probes == ["same-id"] * 4
    assert docker.runs == docker.removed == []


async def test_gateway_poll_cannot_publish_running_during_pending_start(start_db):
    # Given a managed guest is still waiting for its readiness publication.
    _operation, original = await pending_start(start_db)
    await start_db.execute("UPDATE agents SET status='idle' WHERE id=%(id)s", {"id": original.id})
    # When its gateway polls before the start operation finishes.
    touched = await agent_dao.touch_openclaw(replace(original, status="idle"))
    # Then polling does not bypass the managed readiness gate.
    assert touched is not None
    assert touched.status == "idle"


async def test_cancelled_adoption_quiesces_unrecorded_candidate(start_db, guest_surface, monkeypatch):
    # Given the adopted guest was never recorded on its agent row.
    operation, original = await pending_start(start_db)
    docker, _probes = guest_surface
    guest = docker.add(replace(original, container_id="unrecorded"), status="exited")

    async def ready_then_cancel(_client, _reference):
        await PostgresStartStore().cancel(original.id)
        return True

    monkeypatch.setattr("app.services.guest_start.guest_ready", ready_then_cancel)
    # When a later stop cancels the start between resume and publication.
    published = await reconciler.execute_start(operation, original)
    # Then the observed candidate is quiesced even without a creation-operation label.
    assert published is False
    assert guest.state.status == "exited"
    assert await PostgresStartStore().outstanding() == []


async def test_first_ready_pass_does_not_publish_running(start_db, guest_surface, monkeypatch):
    # Given a stopped guest whose readiness has only succeeded once.
    operation, original = await pending_start(start_db)
    docker, probes = guest_surface
    docker.add(replace(original, container_id="awaiting-ready"), status="exited")
    observed = []

    async def between_passes(_seconds):
        state = await start_db.fetchval("SELECT status FROM agents WHERE id=%(id)s", {"id": original.id})
        observed.append((state, len(probes)))

    monkeypatch.setattr(guest_readiness, "sleep", between_passes)
    # When the actual readiness loop and publication execute.
    assert await reconciler.execute_start(operation, original)
    # Then the first pass observes stopped state; running appears only after both passes.
    assert observed == [("stopped", 1)]
    assert await start_db.fetchval("SELECT status FROM agents WHERE id=%(id)s", {"id": original.id}) == "running"


async def test_cancelled_cleanup_discovers_labelled_successor_of_missing_observation(start_db, guest_surface):
    # Given an earlier observation disappeared before a retry created its canonical writer.
    operation, original = await pending_start(start_db)
    docker, _probes = guest_surface
    await PostgresStartStore().observe(operation.operation_id, replace(original, container_id="missing"))
    guest = docker.add(replace(original, container_id="successor"))
    guest.config.labels["maraclaw.start_operation"] = str(operation.operation_id)
    await PostgresStartStore().cancel(original.id)
    # When the cancelled operation cleans up after a process restart.
    await reconciler.cleanup_cancelled_start(operation)
    # Then a stale observed ID cannot orphan its operation-labelled writer.
    assert guest.state.status == "exited"
    assert await PostgresStartStore().outstanding() == []


async def test_prepublication_flush_ambiguity_preserves_recoverable_guest(start_db, guest_surface, monkeypatch):
    # Given a ready guest whose request commit/cache acknowledgement will fail.
    operation, original = await pending_start(start_db)
    docker, _probes = guest_surface
    guest = docker.add(replace(original, container_id="ready-flush"))

    async def interrupted_flush():
        raise OSError("cache acknowledgement lost")

    monkeypatch.setattr(reconciler, "flush_request_transaction", interrupted_flush)
    # When the prepublication flush has an ambiguous outcome.
    assert not await reconciler.execute_start(operation, original)
    # Then authoritative pending state retains the healthy candidate for later recovery.
    current = await agent_start_dao.get(start_db, original.id)
    assert current is not None
    assert current.state == "pending"
    assert guest.state.status == "running"
    assert docker.removed == []
