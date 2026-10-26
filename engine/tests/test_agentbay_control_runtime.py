import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import override

import anyio
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import agentbay_control as control
from app.api.agentbay_control import _perform_drag
from app.core.json_types import JsonObject, json_object_from_response
from app.records.user import UserRecord
from app.services import agentbay_client, agentbay_shared_sessions, take_control_state as state
from app.services.agent_credential_capture import agent_credential_dao
from take_control_fakes import SharedRedis
from take_control_sdk_fakes import SDKStore, install


class DesktopClient(agentbay_client.AgentBayClient):
    def __init__(self) -> None:
        self._image_type: str | None = "linux_latest"
        self.drag_calls: list[tuple[int, int, int, int]] = []

    @override
    async def computer_drag_mouse(
        self, from_x: int, from_y: int, to_x: int, to_y: int, button: str = "left"
    ) -> agentbay_client.AgentBayResponse:
        self.drag_calls.append((from_x, from_y, to_x, to_y))
        return {"success": True}


async def test_perform_drag_uses_computer_api_for_desktop_sessions() -> None:
    client = DesktopClient()
    result = await _perform_drag(client, 10, 20, 30, 40)
    assert client.drag_calls == [(10, 20, 30, 40)]
    assert result == {
        "success": True,
        "method": "computer_drag",
        "output": "Dragged (10,20) -> (30,40)",
    }


@dataclass(slots=True)
class ControlSurface:
    http: AsyncClient
    redis: SharedRedis
    store: SDKStore
    key: state.SessionKey
    user: UserRecord
    token: str
    credentials: list[JsonObject]

    async def post(self, route: str, payload: JsonObject):
        return await self.http.post(f"/agents/{self.key.agent_id}/control/{route}", json=payload)


@pytest.fixture
async def surface(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ControlSurface]:
    redis = SharedRedis()
    store = install(monkeypatch, redis)
    agent_id = uuid.uuid4()
    user = UserRecord(uuid.uuid4())
    key = state.SessionKey(str(agent_id), "chat", "browser")
    await agentbay_client.get_agentbay_client_for_agent(agent_id, "browser", session_id="chat")
    api_cache: agentbay_shared_sessions.ClientCache = {}
    credentials: list[JsonObject] = []

    async def access(_user: UserRecord, _agent_id: uuid.UUID) -> tuple[None, str]:
        return None, "use"

    async def current_user() -> UserRecord:
        return user

    async def get_client(agent_id: uuid.UUID, session_id: str, env_type: str = "browser"):
        return await agentbay_shared_sessions.get_client(
            state.SessionKey(str(agent_id), session_id, env_type), api_cache, create=False
        )

    async def list_credentials(_agent_id: uuid.UUID) -> list[object]:
        return []

    async def create_credential(*, obj_in: JsonObject) -> None:
        credentials.append(obj_in)

    monkeypatch.setattr(control, "check_agent_access", access)
    monkeypatch.setattr(control, "_get_client", get_client)
    monkeypatch.setattr(agent_credential_dao, "list_for_agent", list_credentials)
    monkeypatch.setattr(agent_credential_dao, "create", create_credential)
    monkeypatch.setattr(control, "_tc_interaction_locks", {})
    app = FastAPI()
    app.include_router(control.router)
    app.dependency_overrides[control.get_current_user] = current_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        locked = await http.post(f"/agents/{agent_id}/control/lock", json={"session_id": "chat"})
        assert locked.status_code == 200
        token = json_object_from_response(locked)["owner_token"]
        assert isinstance(token, str)
        yield ControlSurface(http, redis, store, key, user, token, credentials)
    app.dependency_overrides.clear()


CALLS: list[tuple[str, JsonObject]] = [
    ("lock", {}),
    ("unlock", {"platform_hint": "portal.example", "capture": {"mode": "create"}}),
    ("click", {"x": 10, "y": 20}),
    ("type", {"text": "hello"}),
    ("press_keys", {"keys": ["Enter"]}),
    ("drag", {"from_x": 10, "from_y": 20, "to_x": 30, "to_y": 40}),
    ("screenshot", {}),
    ("current-url", {}),
]


@pytest.mark.parametrize(("route", "fields"), CALLS)
async def test_owner_call_renews_before_remote_io(surface: ControlSurface, route: str, fields: JsonObject) -> None:
    surface.redis.advance(119)
    surface.store.before_io = lambda: (
        pytest.fail("remote I/O started before lease renewal")
        if surface.redis.expires[surface.key.lease_key] - surface.redis.now != 120
        else None
    )
    payload: JsonObject = {"session_id": "chat", "owner_token": surface.token, **fields}

    response = await surface.post(route, payload)

    assert response.status_code == 200, response.text
    body = json_object_from_response(response)
    assert body["status"] == {"lock": "locked", "unlock": "unlocked"}.get(route, "ok")
    if route == "current-url":
        assert response.json()["url"] == "https://portal.example/login"
    if route == "screenshot":
        screenshot = body["screenshot"]
        assert isinstance(screenshot, str)
        assert screenshot.startswith("data:image/jpeg;base64,")
    if route == "unlock":
        assert response.json()["cookie_count"] == 1
        assert len(surface.credentials) == 1


@pytest.mark.parametrize(("route", "fields"), CALLS)
@pytest.mark.parametrize("ownership", ["wrong-token", "other-user", "expired", "outage"])
async def test_guard_rejects_before_remote_io(
    surface: ControlSurface, route: str, fields: JsonObject, ownership: str
) -> None:
    payload: JsonObject = {"session_id": "chat", "owner_token": surface.token, **fields}
    if ownership == "wrong-token":
        payload["owner_token"] = "not-owner"
    if ownership == "other-user":
        surface.user.id = uuid.uuid4()
    if ownership == "expired":
        surface.redis.advance(121)
    if ownership == "outage":
        surface.redis.available = False
    surface.store.before_io = lambda: pytest.fail("rejected caller reached remote I/O")

    response = await surface.post(route, payload)

    assert response.status_code == (503 if ownership == "outage" else 409)
    assert response.json()["detail"]["code"] == (
        "take_control_unavailable" if ownership == "outage" else "take_control_lost"
    )
    assert surface.credentials == []


@pytest.mark.parametrize(("route", "fields"), CALLS[1:])
async def test_missing_owner_token_cannot_use_or_release_lease(
    surface: ControlSurface, route: str, fields: JsonObject
) -> None:
    response = await surface.post(route, {"session_id": "chat", **fields})

    assert response.status_code == 409
    assert await state.is_session_locked(surface.key.agent_id, "chat")


async def test_competing_lock_never_discloses_owner_token(surface: ControlSurface) -> None:
    surface.user.id = uuid.uuid4()

    response = await surface.post("lock", {"session_id": "chat"})

    assert response.json()["status"] == "already_locked"
    assert "owner_token" not in response.json()


async def test_lock_does_not_create_blank_session(surface: ControlSurface) -> None:
    response = await surface.post("lock", {"session_id": "unknown-chat"})

    assert response.status_code == 404
    assert len(surface.store.remotes) == 1


async def test_action_rechecks_ownership_after_queue_wait(surface: ControlSurface) -> None:
    lock = control._get_interaction_lock(uuid.UUID(surface.key.agent_id), "chat")
    await lock.acquire()
    responses: list[int] = []

    async def queued_action() -> None:
        response = await surface.post("click", {"session_id": "chat", "owner_token": surface.token, "x": 1, "y": 2})
        responses.append(response.status_code)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(queued_action)
        await anyio.wait_all_tasks_blocked()
        surface.redis.advance(121)
        lock.release()

    assert responses == [409]
    assert surface.store.remotes[0].executed == []
