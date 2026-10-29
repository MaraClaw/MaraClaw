"""No readiness or work contact reaches a guest before its current-boot AgentBootstrapCompleted event."""

import pytest

import test_guest_start_safety as start_fixtures
from app.core.domain.agents import AgentStatus
from app.services import guest_readiness, guest_start
from app.services.guest_admission import GuestStartPreflightError
from app.services.guest_probe import probe_guest
from app.services.openclaw_update_docker import UpgradeDocker
from guest_bootstrap_fakes import CHECK_READY, boot_cli
from openclaw_update_fakes import FakeDocker
from test_openclaw_update_executor import Harness

env = start_fixtures.env


@pytest.fixture
def boot(env, tmp_path, monkeypatch):
    _manager, docker, _agent = env
    cli = boot_cli(tmp_path)
    cli.guest("old", boot="B1", event="B1")
    monkeypatch.setattr(docker, "docker_cmd", [str(cli.executable)], raising=False)
    elapsed = 0.0

    def probe_with_modeled_duration(client, reference, _timeout):
        nonlocal elapsed
        result = probe_guest(client, reference, 5.0)
        elapsed += 1.0
        return result

    monkeypatch.setattr(guest_readiness, "probe_guest", probe_with_modeled_duration)
    monkeypatch.setattr(guest_readiness, "monotonic", lambda: elapsed)
    monkeypatch.setattr(guest_readiness, "READY_TIMEOUT_SECONDS", 3.0)
    monkeypatch.setattr(guest_readiness, "READY_INTERVAL_SECONDS", 0)
    return cli


@pytest.mark.parametrize(
    ("event", "ready"),
    [("B2", True), (None, False), ("B1", False), ("not-an-event", False)],
    ids=["current", "missing", "stale", "malformed"],
)
def test_probe_trusts_only_an_event_for_the_current_boot(tmp_path, event, ready):
    # Given a guest on boot B2 whose event file is current, missing, stale or malformed.
    cli = boot_cli(tmp_path)
    cli.guest("guest", boot="B2", event=event)
    # When the engine asks whether it is ready.
    result = probe_guest(cli.client(), "guest", 5.0)
    # Then only the guest helper's check-ready was exec'd and only a current event is ready.
    assert result is ready
    assert cli.calls() == [["exec", "guest", *CHECK_READY]]


@pytest.mark.parametrize("finished", [False, True])
async def test_stop_start_of_the_same_container_needs_the_new_boot_event(env, boot, monkeypatch, finished):
    # Given a stopped guest whose previous boot already wrote its event.
    manager, docker, agent = env
    docker.guests["old"].state.status = "exited"
    agent.status = "stopped"

    def start(reference):
        docker.inspect(reference).state.status = "running"
        boot.guest(reference, boot="B2", event="B2" if finished else "B1")

    monkeypatch.setattr(docker, "start", start, raising=False)
    # When Start resumes the same container, which bootstraps again on its new boot.
    if finished:
        assert await manager.start_container(agent) == "old"
        assert agent.status == "running"
    else:
        with pytest.raises(GuestStartPreflightError) as failure:
            await manager.start_container(agent)
        assert (failure.value.code, agent.status) == ("readiness_failed", "error")
    # Then the stale event never counted, and nothing but the helper check reached the guest.
    assert {tuple(call) for call in boot.calls()} == {("exec", "old", *CHECK_READY)}
    assert docker.runs == docker.removed == []


async def test_unchanged_run_replay_adopts_the_guest_after_current_boot_checks(env, boot):
    # Given a running guest that finished bootstrapping on its current boot.
    manager, docker, agent = env
    # When Start is replayed.
    results = [await manager.start_container(agent), await manager.start_container(agent)]
    # Then it is adopted after two helper checks per call, with no mutation.
    assert results == ["old", "old"]
    assert boot.calls() == [["exec", "old", *CHECK_READY]] * 4
    assert docker.runs == docker.removed == []


async def test_new_candidate_stays_creating_until_readiness_publishes_running(env, boot, monkeypatch):
    # Given no guest, and a readiness owner that observes the record when the wait begins.
    manager, docker, agent = env
    docker.guests.clear()
    agent.container_id = None
    observed = []
    wait = guest_start.guest_ready

    async def ready(client, reference):
        observed.append(agent.status)
        boot.guest(reference, boot="B1", event="B1")
        return await wait(client, reference)

    monkeypatch.setattr(guest_start, "guest_ready", ready)
    # When Start creates the candidate.
    result = await manager.start_container(agent)
    # Then creation alone did not claim running; the readiness owner did.
    assert result == "created-1"
    assert observed == [AgentStatus.CREATING]
    assert agent.status == AgentStatus.RUNNING


async def test_replacement_candidate_is_creating_not_running(env):
    # Given a replacement such as an upgrade's, which its caller must still verify.
    manager, _docker, agent = env
    # When the candidate is created.
    reference = await manager.replace_container(agent, image="sha256:new", rehydrate=False)
    # Then the record names it but does not claim running.
    assert reference is not None
    assert (agent.container_id, agent.status) == (reference, AgentStatus.CREATING)


@pytest.mark.parametrize(("old_finishes", "code"), [(True, "not_ready"), (False, "rollback_failed")])
async def test_upgrade_and_rollback_readiness_share_the_current_boot_gate(tmp_path, monkeypatch, old_finishes, code):
    # Given a replacement that never writes its event, and an original that restarts on a new boot in rollback.
    cli = boot_cli(tmp_path)
    cli.guest("old-id", boot="B1", event="B1")
    h = Harness(tmp_path)
    h.executor.ready_timeout = 1.0
    start = FakeDocker.start

    def restart(docker, reference):
        start(docker, reference)
        cli.guest(reference, boot="B2", event="B2" if old_finishes else "B1")

    def ready(_docker, reference, timeout):
        return UpgradeDocker(cli.client()).ready(reference, timeout)

    monkeypatch.setattr(FakeDocker, "ready", ready)
    monkeypatch.setattr(FakeDocker, "start", restart)
    # When the cutover fails readiness and rolls back.
    await h.executor.process(h.run.id)
    # Then both phases were judged by the helper check alone, and the original stays published.
    assert (h.stored_run.state, h.stored_run.failure_code) == ("failed", code)
    assert h.stored_agent.container_id == "old-id"
    calls = cli.calls()
    assert calls
    assert all(call[0] == "exec" and call[2:] == CHECK_READY for call in calls)
    probed = {call[1] for call in calls}
    assert "old-id" in probed
    assert len(probed) == 2
