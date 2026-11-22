from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, call
from uuid import uuid4

import pytest
from anyio import get_cancelled_exc_class
from pywhales import DockerClient
from pywhales.exceptions import DockerException

from app.core.domain.agents import AgentStatus
from app.core.domain.users import UserRole
from app.records.agent import AgentRecord
from app.records.agent_runtime_loss import AgentRuntimeLoss
from app.records.agent_start import AgentStartOperation
from app.records.gateway_message import GatewayMessageRecord
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.services import (
    agent_runtime_loss as loss,
    agent_start_reconciler as reconciler,
    guest_image,
    openclaw_gateway_rpc as rpc,
)
from app.services.agent_manager import agent_manager
from app.services.guest_admission import GuestStartPreflightError
from app.services.guest_identity import GuestIdentity, GuestOwnershipError, GuestResolutionCache
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


@pytest.fixture
def runtime_loss_report(monkeypatch):
    agent = guest()
    detected = datetime(2026, 11, 13, tzinfo=UTC)
    configured_image = "configured-openclaw:latest"

    def record(_db, agent_id, reason, *, recorded_container_id, image):
        return AgentRuntimeLoss(agent_id, reason, recorded_container_id, image, detected)

    dao = SimpleNamespace(
        record=AsyncMock(side_effect=record),
        claim_notification=AsyncMock(return_value=True),
        pending_messages=AsyncMock(return_value=3),
    )
    creator = UserRecord(
        id=agent.creator_id,
        tenant_id=agent.tenant_id,
        identity=IdentityRecord(id=uuid4(), must_change_password=True),
    )
    creator_lookup = AsyncMock(return_value=creator)
    admins = AsyncMock(return_value=[])
    send = AsyncMock()
    monkeypatch.setattr(loss, "agent_runtime_loss_dao", dao)
    monkeypatch.setattr(loss, "committed_start_connection", committed)
    monkeypatch.setattr(loss.user_dao, "get_with_identity", creator_lookup)
    monkeypatch.setattr(loss.user_dao, "list_active_platform_admins", admins)
    monkeypatch.setattr(loss.tenant_dao, "get", AsyncMock(return_value=SimpleNamespace(name="Acme")))
    monkeypatch.setattr(loss, "get_settings", lambda: SimpleNamespace(MARACLAW_OPENCLAW_IMAGE=configured_image))
    monkeypatch.setattr(loss, "send_notification", send)
    return SimpleNamespace(
        agent=agent,
        creator=creator,
        creator_lookup=creator_lookup,
        dao=dao,
        admins=admins,
        send=send,
        detected=detected,
        configured_image=configured_image,
    )


@pytest.mark.parametrize(
    ("reason", "admin_reason", "member_action", "creator_role"),
    [
        (
            "container_stopped",
            "The agent's guest container still exists on the Docker daemon but is stopped.",
            "select Start to resume",
            UserRole.MEMBER,
        ),
        (
            "container_missing",
            "The agent's guest container no longer exists on the Docker daemon.",
            "select Start a new container",
            UserRole.MEMBER,
        ),
        (
            "image_unavailable",
            "The guest image the agent is pinned to no longer exists on the Docker daemon.",
            "Wait for the platform team",
            UserRole.PLATFORM_ADMIN,
        ),
    ],
)
async def test_report_alerts_creator_once_and_keeps_platform_admin_alerts(
    runtime_loss_report, reason, admin_reason, member_action, creator_role
):
    # Given a loss that can be claimed once and two admins, one of whom cannot be notified.
    report = runtime_loss_report
    agent = report.agent
    report.creator.role = creator_role
    failed_admin = UserRecord(id=uuid4(), role=UserRole.PLATFORM_ADMIN)
    admin = report.creator if report.creator.is_platform_admin else UserRecord(id=uuid4(), role=UserRole.PLATFORM_ADMIN)
    report.admins.return_value = [failed_admin, admin]
    report.dao.claim_notification.side_effect = [True, False]
    image = "unavailable-openclaw:removed"

    async def send(user_id, **_fields):
        if user_id == failed_admin.id:
            raise RuntimeError("notification store down")

    report.send.side_effect = send
    # When the loss is reported twice.
    await loss.report_runtime_loss(agent, reason, image=image)
    await loss.report_runtime_loss(agent, reason, image=image)
    # Then delivery is once per role, even if both roles target the same user.
    assert report.dao.record.await_count == 2
    report.creator_lookup.assert_awaited_once_with(agent.creator_id, fresh=True)
    assert report.send.await_count == 3
    member_link = f"/app/agents/{agent.id}/settings"
    (member_alert,) = [alert for alert in report.send.await_args_list if alert.kwargs["link"] == member_link]
    assert member_alert.args == (agent.creator_id,)
    assert member_alert.kwargs["notification_kind"] == "system_alert"
    assert member_alert.kwargs["ref_id"] == agent.id
    assert "agent_id" not in member_alert.kwargs
    assert agent.name in member_alert.kwargs["body"]
    assert "Settings" in member_alert.kwargs["body"]
    assert member_action in member_alert.kwargs["body"]
    for field in (agent.container_id, agent.openclaw_image_id, image, report.configured_image, str(agent.tenant_id)):
        assert field not in member_alert.kwargs["title"]
        assert field not in member_alert.kwargs["body"]

    # Both admin attempts still carry the original operations alert.
    stopped = reason == "container_stopped"
    admin_title = f"Agent {agent.name} {'has a stopped guest container' if stopped else 'lost its guest container'}"
    recovery = (
        "Use Start to resume the existing guest container; pending messages are kept and will be answered once it is running."
        if stopped
        else "The agent manager can start a new container from the agent's settings; pending messages are kept and will be answered."
    )
    admin_body = (
        f"Agent: {agent.name} ({agent.id})\nCompany: Acme ({agent.tenant_id})\nReason: {admin_reason}\n"
        f"Recorded container: {agent.container_id}\nUnavailable image: {image}\n"
        f"Pinned image: {agent.openclaw_image_id}\nConfigured image: {report.configured_image}\n"
        f"Detected at: {report.detected.isoformat()}\nPending messages: 3\n\n{recovery}"
    )
    report.send.assert_has_awaits(
        [
            call(
                recipient.id,
                notification_kind="system_alert",
                title=admin_title,
                body=admin_body,
                link=f"/companies/{agent.tenant_id}",
                ref_id=agent.id,
                sender_name="System",
            )
            for recipient in (failed_admin, admin)
        ],
        any_order=True,
    )


@pytest.mark.parametrize(
    ("claimed", "member_active", "identity_active"),
    [
        pytest.param(False, True, True, id="lost-claim"),
        pytest.param(True, False, True, id="inactive-member"),
        pytest.param(True, True, False, id="inactive-identity"),
        pytest.param(True, None, True, id="missing-member"),
    ],
)
async def test_report_skips_creator_without_claim_or_active_principal(
    runtime_loss_report, claimed, member_active, identity_active
):
    report = runtime_loss_report
    report.dao.claim_notification.return_value = claimed
    if member_active is None:
        report.creator_lookup.return_value = None
    else:
        report.creator.is_active = member_active
        report.creator.identity.is_active = identity_active
    await loss.report_runtime_loss(report.agent, "container_missing")
    report.send.assert_not_awaited()


@pytest.mark.parametrize("change", ["other-company", "no-company", "no-identity"])
async def test_report_skips_creator_who_cannot_reach_the_agent_company(runtime_loss_report, change):
    report = runtime_loss_report
    if change == "other-company":
        report.creator.tenant_id = uuid4()
    elif change == "no-company":
        report.creator.tenant_id = None
    else:
        report.creator.identity = None
    await loss.report_runtime_loss(report.agent, "container_missing")
    report.send.assert_not_awaited()


async def test_creator_notification_failure_does_not_escape_or_block_admin_alert(runtime_loss_report):
    report = runtime_loss_report
    admin = UserRecord(id=uuid4(), role=UserRole.PLATFORM_ADMIN)
    report.admins.return_value = [admin]

    async def send(user_id, **_fields):
        if user_id == report.agent.creator_id:
            raise RuntimeError("notification store down")

    report.send.side_effect = send
    await loss.report_runtime_loss(report.agent, "container_missing")
    assert {alert.args for alert in report.send.await_args_list} == {(admin.id,), (report.agent.creator_id,)}


async def test_admin_lookup_failure_does_not_block_creator_alert(runtime_loss_report):
    report = runtime_loss_report
    report.admins.side_effect = RuntimeError("admin lookup failed")
    await loss.report_runtime_loss(report.agent, "container_missing")
    assert [alert.args for alert in report.send.await_args_list] == [(report.agent.creator_id,)]


async def test_creator_notification_cancellation_escapes(runtime_loss_report):
    cancelled = get_cancelled_exc_class()
    runtime_loss_report.send.side_effect = cancelled
    with pytest.raises(cancelled):
        await loss.report_runtime_loss(runtime_loss_report.agent, "container_missing")


async def test_recording_failure_does_not_escape_or_send_alerts(runtime_loss_report):
    runtime_loss_report.dao.record.side_effect = RuntimeError("database down")
    await loss.report_runtime_loss(runtime_loss_report.agent, "container_missing")
    runtime_loss_report.send.assert_not_awaited()


@pytest.mark.parametrize(
    ("reporter", "reason"),
    [("report_missing_guest", "container_missing"), ("report_stopped_guest", "container_stopped")],
)
@pytest.mark.parametrize(
    ("status", "start_state", "upgrading", "reported"),
    [
        (AgentStatus.RUNNING, None, False, True),
        (AgentStatus.RUNNING, "failed", False, True),
        (AgentStatus.STOPPED, None, False, False),
        (AgentStatus.IDLE, None, False, False),
        (AgentStatus.CREATING, None, False, False),
        (AgentStatus.ERROR, None, False, False),
        (AgentStatus.RUNNING, "pending", False, False),
        (AgentStatus.RUNNING, None, True, False),
    ],
)
async def test_unavailable_guest_is_reported_only_for_a_running_agent_without_start_or_upgrade(
    monkeypatch, reporter, reason, status, start_state, upgrading, reported
):
    # Given dispatch found no owned guest for an agent in a given recorded state.
    agent = guest(status)
    operation = None if start_state is None else AgentStartOperation(agent.id, uuid4(), start_state)
    report = AsyncMock()
    monkeypatch.setattr(loss, "committed_start_connection", committed)
    monkeypatch.setattr(loss.agent_start_dao, "get", AsyncMock(return_value=operation))
    monkeypatch.setattr(loss.openclaw_update_notice_dao, "gate_state", AsyncMock(return_value=(upgrading, True)))
    monkeypatch.setattr(loss, "report_runtime_loss", report)
    # When the unavailable guest is reported.
    await getattr(loss, reporter)(agent)
    # Then only an unexplained absence is recorded as a lost container.
    if reported:
        report.assert_awaited_once_with(agent, reason)
    else:
        report.assert_not_awaited()


@pytest.mark.parametrize("reporter", ["report_missing_guest", "report_stopped_guest"])
@pytest.mark.parametrize("failure", ["connection", "start", "upgrade"])
async def test_unavailable_guest_guard_failure_does_not_report_or_escape(monkeypatch, reporter, failure):
    agent, report = guest(), AsyncMock()

    @asynccontextmanager
    async def unavailable_connection():
        raise RuntimeError("database unavailable")
        yield

    monkeypatch.setattr(
        loss, "committed_start_connection", unavailable_connection if failure == "connection" else committed
    )
    monkeypatch.setattr(
        loss.agent_start_dao,
        "get",
        AsyncMock(return_value=None, side_effect=RuntimeError("start lookup failed") if failure == "start" else None),
    )
    monkeypatch.setattr(
        loss.openclaw_update_notice_dao,
        "gate_state",
        AsyncMock(
            return_value=(False, True),
            side_effect=RuntimeError("upgrade lookup failed") if failure == "upgrade" else None,
        ),
    )
    monkeypatch.setattr(loss, "report_runtime_loss", report)
    await getattr(loss, reporter)(agent)
    report.assert_not_awaited()


async def test_running_clear_is_conditional_and_best_effort(monkeypatch):
    agent_id = uuid4()
    cleared = AsyncMock(side_effect=[None, RuntimeError("database down")])
    monkeypatch.setattr(loss, "committed_start_connection", committed)
    monkeypatch.setattr(loss.agent_runtime_loss_dao, "clear_stopped", cleared)
    await loss.clear_stopped_guest_loss(agent_id)
    await loss.clear_stopped_guest_loss(agent_id)
    assert cleared.await_count == 2
    assert all(call.args[1] == agent_id for call in cleared.await_args_list)


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize(
    ("observation", "running", "restarting", "reason"),
    [
        ("absent", False, False, "container_missing"),
        ("exited", False, False, "container_stopped"),
        ("created", False, None, "container_stopped"),
        ("exited", False, True, None),
        ("created", False, True, None),
        ("restarting", False, True, None),
        ("dead", False, False, None),
        ("paused", False, False, None),
        ("removing", False, False, None),
        ("unknown", False, False, None),
        ("no_state", False, False, None),
        ("docker_error", False, False, None),
        ("inspect_error", False, False, None),
        ("foreign", False, False, None),
    ],
)
async def test_dispatch_reports_only_proven_missing_or_resumable_stopped_guests(
    monkeypatch, cached, observation, running, restarting, reason
):
    # Given an owned resolution (possibly cached), or a failed/foreign resolution.
    agent = guest()
    message = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="hello")
    report = AsyncMock()
    cleared = AsyncMock()
    cache = GuestResolutionCache(30)
    owned = GuestIdentity("owned", "owned", str(agent.id), ())
    if cached and observation not in ("absent", "docker_error", "foreign"):
        cache.put(agent, owned)

    @asynccontextmanager
    async def exclusion(_agent_id):
        yield

    def resolve(*_args):
        if observation == "docker_error":
            raise DockerException(["docker", "inspect"], 1)
        if observation == "foreign":
            raise GuestOwnershipError(agent.id, "foreign-container", str(uuid4()))
        return None if observation == "absent" else owned

    def inspect(_reference):
        if observation == "inspect_error":
            raise DockerException(["docker", "inspect"], 1)
        return SimpleNamespace(state=state)

    state = (
        None
        if observation == "no_state"
        else SimpleNamespace(status=observation, running=running, restarting=restarting)
    )
    monkeypatch.setattr(rpc, "guest_write_section", exclusion)
    monkeypatch.setattr(rpc, "assert_work_allowed", AsyncMock(return_value=None))
    monkeypatch.setattr(rpc.agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr(rpc, "resolve_guest", resolve)
    monkeypatch.setattr(rpc, "guest_resolutions", cache)
    monkeypatch.setattr(rpc, "clear_stopped_guest_loss", cleared)
    monkeypatch.setattr(loss, "committed_start_connection", committed)
    monkeypatch.setattr(loss.agent_start_dao, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(loss.openclaw_update_notice_dao, "gate_state", AsyncMock(return_value=(False, False)))
    monkeypatch.setattr(loss, "report_runtime_loss", report)
    monkeypatch.setattr(agent_manager, "docker_client", SimpleNamespace(container=SimpleNamespace(inspect=inspect)))
    # When the turn is dispatched.
    outcome = await rpc.execute_gateway_turn(agent, message)
    # Then only proven loss records a reason; every refusal keeps its exact admission result and drops the cache.
    assert outcome == rpc.NotAdmittedOutcome(str(message.id), "not_submitted", "guest_unavailable")
    if reason is None:
        report.assert_not_awaited()
    else:
        report.assert_awaited_once_with(agent, reason)
    assert cache.get(agent) is None
    cleared.assert_not_awaited()


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
