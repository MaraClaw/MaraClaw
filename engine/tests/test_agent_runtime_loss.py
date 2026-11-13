from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pywhales import DockerClient
from pywhales.exceptions import DockerException

from app.core.domain.agents import AgentStatus
from app.records.agent import AgentRecord
from app.records.agent_runtime_loss import AgentRuntimeLoss
from app.records.agent_start import AgentStartOperation
from app.records.gateway_message import GatewayMessageRecord
from app.records.user import UserRecord
from app.services import (
    agent_runtime_loss as loss,
    agent_start_reconciler as reconciler,
    guest_image,
    openclaw_gateway_rpc as rpc,
)
from app.services.agent_manager import agent_manager
from app.services.guest_admission import GuestStartPreflightError
from app.services.openclaw_native_capacity import LaneKey, shutdown_native_capacity
from app.services.openclaw_release_backoff import ReleaseBackoff
from app.services.openclaw_update_snapshot import commit_snapshot, snapshot_path


@pytest.fixture(autouse=True)
async def close_native_threads():
    yield
    shutdown_native_capacity()


def guest(status=AgentStatus.RUNNING):
    return AgentRecord(
        id=uuid4(),
        name="Guest",
        creator_id=uuid4(),
        tenant_id=uuid4(),
        status=status,
        container_id="lost-container",
        openclaw_image_id="sha256:gone",
    )


@asynccontextmanager
async def committed():
    yield object()


class Images:
    def __init__(self, present=(), pull_error=None):
        self.present = set(present)
        self.pull_error = pull_error
        self.pulled = []

    def exists(self, reference):
        return reference in self.present

    def pull(self, reference, **_options):
        self.pulled.append(reference)
        if self.pull_error is not None:
            raise self.pull_error


def docker(monkeypatch, images):
    client = DockerClient()
    monkeypatch.setattr(client, "image", images)
    return client


@pytest.mark.parametrize("image", ["sha256:gone", "arm64-openclaw:rebuilt"])
async def test_missing_local_image_or_id_is_reported_as_unavailable_without_a_pull(monkeypatch, image):
    # Given a daemon without a local reference or image id that the engine never pulls.
    images = Images()
    # When the guest image is settled before admission.
    with pytest.raises(guest_image.GuestImageUnavailableError) as raised:
        await guest_image.ensure_guest_image(docker(monkeypatch, images), image)
    # Then the typed error names the image and keeps the existing failure code.
    assert (raised.value.image, raised.value.code) == (image, "runtime_failed")
    assert images.pulled == []


async def test_pull_failure_stays_a_generic_preflight_failure(monkeypatch):
    # Given a registry reference whose pull fails.
    images = Images(pull_error=DockerException(["docker", "pull"], 1))
    # When the guest image is settled.
    with pytest.raises(GuestStartPreflightError) as raised:
        await guest_image.ensure_guest_image(docker(monkeypatch, images), "registry.example/openclaw:1")
    # Then it is not reported as a lost local image.
    assert not isinstance(raised.value, guest_image.GuestImageUnavailableError)
    assert raised.value.code == "runtime_failed"
    assert images.pulled == ["registry.example/openclaw:1"]


async def test_report_alerts_every_platform_admin_once_and_never_raises(monkeypatch):
    # Given a lost guest, two platform admins (one of whom cannot be notified) and a claim that succeeds once.
    agent = guest()
    detected = datetime(2026, 11, 13, tzinfo=UTC)
    recorded = AgentRuntimeLoss(agent.id, "image_unavailable", agent.container_id, "sha256:gone", detected)
    claims = iter([True, False])
    dao = SimpleNamespace(
        record=AsyncMock(return_value=recorded),
        claim_notification=AsyncMock(side_effect=lambda *_: next(claims)),
        pending_messages=AsyncMock(return_value=3),
    )
    admins = [UserRecord(id=uuid4()), UserRecord(id=uuid4())]
    sent = []

    async def send(user_id, **fields):
        if user_id == admins[0].id:
            raise RuntimeError("mail store down")
        sent.append((user_id, fields))

    monkeypatch.setattr(loss, "agent_runtime_loss_dao", dao)
    monkeypatch.setattr(loss, "committed_start_connection", committed)
    monkeypatch.setattr(loss.user_dao, "list_active_platform_admins", AsyncMock(return_value=admins))
    monkeypatch.setattr(loss.tenant_dao, "get", AsyncMock(return_value=SimpleNamespace(name="Acme")))
    monkeypatch.setattr(loss, "send_notification", send)
    # When the loss is reported twice.
    await loss.report_runtime_loss(agent, "image_unavailable", image="sha256:gone")
    await loss.report_runtime_loss(agent, "image_unavailable", image="sha256:gone")
    # Then only the claimed report alerts, the failed recipient does not block the other, and no row targets the agent.
    assert dao.record.await_count == 2
    assert [user_id for user_id, _fields in sent] == [admins[1].id]
    fields = sent[0][1]
    assert "agent_id" not in fields
    assert (fields["ref_id"], fields["link"], fields["sender_name"]) == (
        agent.id,
        f"/companies/{agent.tenant_id}",
        "System",
    )
    assert fields["notification_kind"] == "system_alert"
    assert len(fields["title"]) <= 200
    assert "Pending messages: 3" in fields["body"]
    # And a storage failure is contained.
    monkeypatch.setattr(dao, "record", AsyncMock(side_effect=RuntimeError("database down")))
    await loss.report_runtime_loss(agent, "container_missing")


@pytest.mark.parametrize(
    ("status", "start_state", "upgrading", "reported"),
    [
        (AgentStatus.RUNNING, None, False, True),
        (AgentStatus.RUNNING, "failed", False, True),
        (AgentStatus.STOPPED, None, False, False),
        (AgentStatus.RUNNING, "pending", False, False),
        (AgentStatus.RUNNING, None, True, False),
    ],
)
async def test_missing_guest_is_reported_only_for_a_running_agent_without_start_or_upgrade(
    monkeypatch, status, start_state, upgrading, reported
):
    # Given dispatch found no owned guest for an agent in a given recorded state.
    agent = guest(status)
    operation = None if start_state is None else AgentStartOperation(agent.id, uuid4(), start_state)
    report = AsyncMock()
    monkeypatch.setattr(loss, "committed_start_connection", committed)
    monkeypatch.setattr(loss.agent_start_dao, "get", AsyncMock(return_value=operation))
    monkeypatch.setattr(loss.openclaw_update_notice_dao, "gate_state", AsyncMock(return_value=(upgrading, False)))
    monkeypatch.setattr(loss, "report_runtime_loss", report)
    # When the missing guest is reported.
    await loss.report_missing_guest(agent)
    # Then only an unexplained absence is recorded as a lost container.
    if reported:
        report.assert_awaited_once_with(agent, "container_missing")
    else:
        report.assert_not_awaited()


@pytest.mark.parametrize("observation", ["absent", "stopped", "docker_error"])
async def test_dispatch_reports_only_an_absent_guest_and_keeps_its_outcome(monkeypatch, observation):
    # Given a native turn and a guest that is absent, stopped, or cannot be inspected.
    agent = guest()
    message = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="hello")
    report = AsyncMock()

    @asynccontextmanager
    async def exclusion(_agent_id):
        yield

    def resolve(*_args):
        if observation == "docker_error":
            raise DockerException(["docker", "inspect"], 1)
        return None if observation == "absent" else SimpleNamespace(id="owned")

    stopped = SimpleNamespace(state=SimpleNamespace(running=False))
    monkeypatch.setattr(rpc, "guest_write_section", exclusion)
    monkeypatch.setattr(rpc, "assert_work_allowed", AsyncMock(return_value=None))
    monkeypatch.setattr(rpc.agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr(rpc, "resolve_guest", resolve)
    monkeypatch.setattr(rpc, "report_missing_guest", report)
    monkeypatch.setattr(
        agent_manager, "docker_client", SimpleNamespace(container=SimpleNamespace(inspect=lambda _id: stopped))
    )
    # When the turn is dispatched.
    outcome = await rpc.execute_gateway_turn(agent, message)
    # Then the outcome is unchanged and only an absent guest is reported.
    assert outcome == rpc.NotAdmittedOutcome(str(message.id), "not_submitted", "guest_unavailable")
    assert report.await_count == (1 if observation == "absent" else 0)


async def test_publication_clears_loss_forgets_backoff_drops_snapshot_and_wakes(monkeypatch, tmp_path):
    # Given a recorded loss, backed-off lanes for this and another agent, and a crashed recovery snapshot.
    agent = guest()
    other = LaneKey(uuid4(), "conversation")
    backoff = ReleaseBackoff(lambda: 100.0)
    for lane in (LaneKey(agent.id, "conversation"), LaneKey(agent.id, "shared"), other):
        _ = backoff.released(lane)
    agent_dir = tmp_path / str(agent.id)
    agent_dir.mkdir()
    _ = commit_snapshot(agent_dir, loss.recovery_snapshot_id(agent.id))
    cleared, woken = AsyncMock(), []
    monkeypatch.setattr(loss, "clear_runtime_loss", cleared)
    monkeypatch.setattr(loss, "release_backoff", backoff)
    monkeypatch.setattr(loss, "wake_agent_lanes", woken.append)
    # When a start publishes the new guest.
    await loss.guest_published(agent, agent_dir)
    # Then held messages of this agent are eligible now and the stale snapshot is gone.
    cleared.assert_awaited_once_with(agent.id)
    assert not backoff.waiting(LaneKey(agent.id, "conversation"))
    assert not backoff.waiting(LaneKey(agent.id, "shared"))
    assert backoff.waiting(other)
    assert woken == [agent]
    assert not snapshot_path(agent_dir, loss.recovery_snapshot_id(agent.id)).exists()


async def test_unavailable_image_start_records_loss_and_keeps_runtime_failed(monkeypatch):
    # Given a start whose pinned image no longer exists.
    agent = guest()
    operation = AgentStartOperation(agent.id, uuid4(), "pending")
    failed, report, published = AsyncMock(), AsyncMock(), AsyncMock()

    async def start(_agent):
        raise guest_image.GuestImageUnavailableError("missing", code="runtime_failed", image="sha256:gone")

    monkeypatch.setattr(reconciler.start_store, "reserve", AsyncMock(return_value=True))
    monkeypatch.setattr(reconciler.start_store, "fail", failed)
    monkeypatch.setattr(reconciler.agent_manager, "start_container", start)
    monkeypatch.setattr(reconciler, "report_runtime_loss", report)
    monkeypatch.setattr(reconciler, "guest_published", published)
    # When the start executes.
    with pytest.raises(guest_image.GuestImageUnavailableError):
        await reconciler.execute_start(operation, agent)
    # Then the loss is recorded and the retained intent fails with the unchanged code.
    report.assert_awaited_once_with(agent, "image_unavailable", image="sha256:gone")
    failed.assert_awaited_once_with(operation, "runtime_failed")
    published.assert_not_awaited()
