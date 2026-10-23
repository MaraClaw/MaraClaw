"""HTTP contract of the OpenClaw upgrade endpoints (frozen shapes, RBAC, replay, accessible-agent filtering)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import TypedDict, Unpack
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.api.openclaw_updates import router
from app.core import access_cache
from app.core.security import get_current_user
from app.records.agent import AgentRecord
from app.records.openclaw_update import OpenClawUpdateNoticeRecord, OpenClawUpdateRunRecord, RunState
from app.records.user import UserRecord
from app.services import openclaw_update_service as service

CREATED = datetime(2026, 10, 22, 3, 0, tzinfo=UTC)
TENANT = uuid.uuid4()


def notice(*, mandatory: bool = False, recipients: int = 3) -> OpenClawUpdateNoticeRecord:
    return OpenClawUpdateNoticeRecord(
        id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        title="OpenClaw 2026.10.5",
        body="Faster gateway",
        mandatory=mandatory,
        recipients=recipients,
        created_at=CREATED,
    )


def run(
    state: RunState = "pending", *, notice_id: uuid.UUID | None = None, agent_id: uuid.UUID | None = None, code=None
):
    return OpenClawUpdateRunRecord(
        id=uuid.uuid4(),
        notice_id=notice_id or uuid.uuid4(),
        agent_id=agent_id or uuid.uuid4(),
        state=state,
        failure_code=code,
    )


class AgentFields(TypedDict, total=False):
    name: str
    tenant_id: uuid.UUID | None
    status: str
    container_id: str | None
    setup_pending: bool
    access_mode: str
    company_access_level: str


def agent(**fields: Unpack[AgentFields]) -> AgentRecord:
    base = AgentRecord(
        id=uuid.uuid4(), name="Ada", creator_id=uuid.uuid4(), tenant_id=TENANT, status="running", container_id="c1"
    )
    return replace(base, **fields)


@pytest.fixture
def actor() -> dict[str, UserRecord]:
    return {"user": UserRecord(id=uuid.uuid4(), tenant_id=TENANT, role="member", display_name="Mo")}


@pytest.fixture
async def client(actor: dict[str, UserRecord]) -> AsyncIterator[httpx.AsyncClient]:
    application = FastAPI()
    application.include_router(router, prefix="/api")
    application.dependency_overrides[get_current_user] = lambda: actor["user"]
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as session:
        yield session


def publish_body(**overrides: object) -> dict[str, object]:
    return {"title": "Upgrade", "body": "Please", "mandatory": True, "request_id": str(uuid.uuid4()), **overrides}


@pytest.mark.parametrize("role", ["member", "agent_admin", "org_admin"])
async def test_only_platform_admins_publish(client, actor, monkeypatch, role):
    actor["user"] = UserRecord(id=uuid.uuid4(), tenant_id=TENANT, role=role)
    publish = AsyncMock()
    monkeypatch.setattr(service.openclaw_update_notice_dao, "publish", publish)
    response = await client.post("/api/admin/openclaw-updates", json=publish_body())
    assert response.status_code == 403
    publish.assert_not_awaited()


async def test_publish_returns_201_then_200_replay_with_the_frozen_shape(client, actor, monkeypatch):
    actor["user"] = UserRecord(id=uuid.uuid4(), tenant_id=TENANT, role="platform_admin", display_name="Root")
    stored = notice(mandatory=True, recipients=7)
    publish = AsyncMock(side_effect=[(stored, True), (stored, False)])
    monkeypatch.setattr(service.openclaw_update_notice_dao, "publish", publish)
    body = publish_body()
    first = await client.post("/api/admin/openclaw-updates", json=body)
    replay = await client.post("/api/admin/openclaw-updates", json=body)
    assert (first.status_code, replay.status_code) == (201, 200)
    expected = {
        "notice": {
            "id": str(stored.id),
            "title": "OpenClaw 2026.10.5",
            "body": "Faster gateway",
            "mandatory": True,
            "created_at": "2026-10-22T03:00:00Z",
        },
        "recipients": 7,
    }
    assert first.json() == replay.json() == expected
    sent = publish.await_args_list[0].kwargs
    assert str(sent["request_id"]) == body["request_id"]
    assert sent["sender_name"] == "Root"


@pytest.mark.parametrize(
    "bad",
    [{"title": "x" * 201}, {"body": "x" * 1001}, {"title": ""}, {"request_id": "not-a-uuid"}, {"mandatory": "maybe"}],
)
async def test_publish_validates_the_frozen_limits(client, actor, bad):
    actor["user"] = UserRecord(id=uuid.uuid4(), tenant_id=TENANT, role="platform_admin")
    response = await client.post("/api/admin/openclaw-updates", json=publish_body(**bad))
    assert response.status_code == 422


@pytest.mark.parametrize(("level", "can_upgrade"), [("use", False), ("manage", True)])
async def test_agent_update_is_readable_with_use_access_and_manage_decides_can_upgrade(
    client, monkeypatch, level, can_upgrade
):
    target = agent()
    mandatory = notice(mandatory=True)
    failed = run("failed", notice_id=mandatory.id, agent_id=target.id, code="not_ready")
    monkeypatch.setattr("app.api.openclaw_updates.check_agent_access", AsyncMock(return_value=(target, level)))
    monkeypatch.setattr(service.openclaw_update_notice_dao, "selected_for_agent", AsyncMock(return_value=mandatory))
    monkeypatch.setattr(service.openclaw_update_run_dao, "active_for_agent", AsyncMock(return_value=None))
    monkeypatch.setattr(service.openclaw_update_run_dao, "latest_for_notice", AsyncMock(return_value=failed))
    response = await client.get(f"/api/agents/{target.id}/openclaw-update")
    assert response.status_code == 200
    assert response.json() == {
        "notice": {
            "id": str(mandatory.id),
            "title": mandatory.title,
            "body": mandatory.body,
            "mandatory": True,
            "created_at": "2026-10-22T03:00:00Z",
        },
        "run": {"id": str(failed.id), "state": "failed", "failure_code": "not_ready"},
        "required": True,
        "can_upgrade": can_upgrade,
    }


async def test_agent_update_without_notice_or_run_is_all_empty(client, monkeypatch):
    target = agent()
    monkeypatch.setattr("app.api.openclaw_updates.check_agent_access", AsyncMock(return_value=(target, "use")))
    monkeypatch.setattr(service.openclaw_update_notice_dao, "selected_for_agent", AsyncMock(return_value=None))
    monkeypatch.setattr(service.openclaw_update_run_dao, "active_for_agent", AsyncMock(return_value=None))
    response = await client.get(f"/api/agents/{target.id}/openclaw-update")
    assert response.json() == {"notice": None, "run": None, "required": False, "can_upgrade": False}


async def test_an_active_run_is_reported_even_for_a_different_notice(client, monkeypatch):
    target = agent()
    shown = notice(mandatory=True)
    active = run("cutover", agent_id=target.id)
    monkeypatch.setattr("app.api.openclaw_updates.check_agent_access", AsyncMock(return_value=(target, "manage")))
    monkeypatch.setattr(service.openclaw_update_notice_dao, "selected_for_agent", AsyncMock(return_value=shown))
    monkeypatch.setattr(service.openclaw_update_run_dao, "active_for_agent", AsyncMock(return_value=active))
    latest = AsyncMock()
    monkeypatch.setattr(service.openclaw_update_run_dao, "latest_for_notice", latest)
    body = (await client.get(f"/api/agents/{target.id}/openclaw-update")).json()
    assert body["run"] == {"id": str(active.id), "state": "cutover", "failure_code": None}
    latest.assert_not_awaited()


class UpgradeFixture:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, level: str = "manage", **fields: Unpack[AgentFields]) -> None:
        self.agent = agent(**fields)
        self.notice = notice()
        self.create = AsyncMock()
        self.succeeded = AsyncMock(return_value=None)
        self.active = AsyncMock(return_value=None)
        monkeypatch.setattr("app.api.openclaw_updates.check_agent_access", AsyncMock(return_value=(self.agent, level)))
        monkeypatch.setattr(service.openclaw_update_notice_dao, "get", AsyncMock(return_value=self.notice))
        monkeypatch.setattr(service.openclaw_update_run_dao, "succeeded_for", self.succeeded)
        monkeypatch.setattr(service.openclaw_update_run_dao, "active_for_agent", self.active)
        monkeypatch.setattr(service.openclaw_update_run_dao, "create_pending", self.create)

    async def post(self, client: httpx.AsyncClient, notice_id: uuid.UUID | None = None) -> httpx.Response:
        return await client.post(
            f"/api/agents/{self.agent.id}/openclaw-update", json={"notice_id": str(notice_id or self.notice.id)}
        )


async def test_manager_starts_an_upgrade_and_gets_202(client, monkeypatch):
    fx = UpgradeFixture(monkeypatch)
    created = run("pending", notice_id=fx.notice.id, agent_id=fx.agent.id)
    fx.create.return_value = created
    response = await fx.post(client)
    assert response.status_code == 202
    assert response.json() == {"id": str(created.id), "state": "pending", "failure_code": None}


async def test_use_only_access_cannot_upgrade(client, monkeypatch):
    fx = UpgradeFixture(monkeypatch, level="use")
    response = await fx.post(client)
    assert response.status_code == 403
    fx.create.assert_not_awaited()


async def test_existing_active_or_succeeded_run_replays_with_200(client, monkeypatch):
    fx = UpgradeFixture(monkeypatch)
    done = run("succeeded", notice_id=fx.notice.id, agent_id=fx.agent.id)
    fx.succeeded.return_value = done
    replay_done = await fx.post(client)
    fx.succeeded.return_value = None
    busy = run("cutover", agent_id=fx.agent.id)
    fx.active.return_value = busy
    replay_busy = await fx.post(client)
    assert (replay_done.status_code, replay_done.json()["id"]) == (200, str(done.id))
    assert (replay_busy.status_code, replay_busy.json()["id"]) == (200, str(busy.id))
    fx.create.assert_not_awaited()


@pytest.mark.parametrize("fields", [{"status": "stopped"}, {"container_id": None}, {"setup_pending": True}])
async def test_stopped_agents_get_409_not_running(client, monkeypatch, fields):
    fx = UpgradeFixture(monkeypatch, **fields)
    response = await fx.post(client)
    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "openclaw_update_not_running"}}
    fx.create.assert_not_awaited()


async def test_unknown_notice_is_404_and_a_lost_insert_race_replays_the_winner(client, monkeypatch):
    fx = UpgradeFixture(monkeypatch)
    monkeypatch.setattr(service.openclaw_update_notice_dao, "get", AsyncMock(return_value=None))
    assert (await fx.post(client)).status_code == 404
    monkeypatch.setattr(service.openclaw_update_notice_dao, "get", AsyncMock(return_value=fx.notice))
    winner = run("pending", agent_id=fx.agent.id)
    fx.create.return_value = None
    fx.active.side_effect = [None, winner]
    response = await fx.post(client)
    assert (response.status_code, response.json()["id"]) == (200, str(winner.id))


async def test_notice_page_lists_only_accessible_agents_with_their_own_state(client, actor, monkeypatch):
    shown = notice(mandatory=True)
    managed, shared, hidden = agent(name="Mine"), agent(name="Shared"), agent(name="Foreign", tenant_id=uuid.uuid4())
    levels = {managed.id: "manage", shared.id: "use", hidden.id: None}
    monkeypatch.setattr(service.openclaw_update_notice_dao, "get", AsyncMock(return_value=shown))
    monkeypatch.setattr(service, "list_visible_agents", AsyncMock(return_value=[managed, shared, hidden]))
    monkeypatch.setattr(service, "compute_agent_access_level", AsyncMock(side_effect=lambda _u, a: levels[a.id]))
    monkeypatch.setattr(service.openclaw_update_notice_dao, "selected_for_agent", AsyncMock(return_value=shown))
    monkeypatch.setattr(service.openclaw_update_run_dao, "active_for_agent", AsyncMock(return_value=None))
    monkeypatch.setattr(service.openclaw_update_run_dao, "latest_for_notice", AsyncMock(return_value=None))
    response = await client.get(f"/api/openclaw-updates/{shown.id}")
    body = response.json()
    assert body["notice"]["id"] == str(shown.id)
    assert [
        (a["agent_id"], a["name"], a["status"], a["update"]["can_upgrade"], a["update"]["required"])
        for a in body["agents"]
    ] == [
        (str(managed.id), "Mine", "running", True, True),
        (str(shared.id), "Shared", "running", False, True),
    ]
    assert str(hidden.id) not in response.text


async def test_unknown_notice_page_is_404(client, monkeypatch):
    monkeypatch.setattr(service.openclaw_update_notice_dao, "get", AsyncMock(return_value=None))
    assert (await client.get(f"/api/openclaw-updates/{uuid.uuid4()}")).status_code == 404


async def test_cross_tenant_agent_is_not_readable_or_upgradable_through_real_access_checks(client, actor, monkeypatch):
    # Given the real access decision with a caller from another tenant.
    from app.core import permissions

    foreign = agent(tenant_id=uuid.uuid4(), access_mode="company", company_access_level="manage")
    caller = actor["user"]
    monkeypatch.setattr(permissions.user_dao, "get", AsyncMock(return_value=caller))
    monkeypatch.setattr(permissions.agent_dao, "get", AsyncMock(return_value=foreign))
    monkeypatch.setattr(access_cache, "read_acl_version", AsyncMock(return_value=0))
    monkeypatch.setattr(access_cache, "get_cached_level", AsyncMock(return_value=None))
    monkeypatch.setattr(access_cache, "set_cached_level", AsyncMock())
    monkeypatch.setattr(access_cache, "memo_get", lambda *_args: None)
    selected = AsyncMock()
    create = AsyncMock()
    monkeypatch.setattr(service.openclaw_update_notice_dao, "selected_for_agent", selected)
    monkeypatch.setattr(service.openclaw_update_run_dao, "create_pending", create)
    read = await client.get(f"/api/agents/{foreign.id}/openclaw-update")
    write = await client.post(f"/api/agents/{foreign.id}/openclaw-update", json={"notice_id": str(uuid.uuid4())})
    assert (read.status_code, write.status_code) == (403, 403)
    selected.assert_not_awaited()
    create.assert_not_awaited()
