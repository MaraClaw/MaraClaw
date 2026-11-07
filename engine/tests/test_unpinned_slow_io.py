"""Slow upload and probe I/O runs with no pooled connection while a single-slot pool still serves other requests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
from tests.test_transaction_boundaries import pool
from tests.transaction_pool_fakes import CountingPool

from app import main
from app.api import atlassian, departments, files as files_api, skills, upload as upload_api
from app.core import outbound_policy
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.dao.user_dao import user_dao
from app.db.session import get_connection
from app.records.agent import AgentRecord
from app.records.identity import IdentityRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services import agentbay_client, email_service, mcp_client, openclaw_update_exclusion as exclusion

__all__ = ["pool"]

type Request = Callable[[httpx.AsyncClient], Awaitable[httpx.Response]]


@dataclass
class Probe:
    """What the blocked external call saw, plus the authority checks that ran before it."""

    pool: CountingPool
    entered: anyio.Event = field(default_factory=anyio.Event)
    release: anyio.Event = field(default_factory=anyio.Event)
    events: list[tuple[str, bool, int]] = field(default_factory=list)

    def authority(self, name: str) -> None:
        self.events.append((name, get_connection() is not None, self.pool.active))

    async def external(self, name: str) -> None:
        self.events.append((name, get_connection() is not None, self.pool.active))
        self.entered.set()
        with anyio.fail_after(5):
            await self.release.wait()


class FakeStorage:
    def __init__(self, probe: Probe) -> None:
        self._probe = probe

    async def create_bytes_if_absent(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        await self._probe.external("storage")
        return SimpleNamespace(ok=True, conflict=False)

    async def write_bytes(self, *_args: object, **_kwargs: object) -> None:
        await self._probe.external("storage")

    async def write_text(self, *_args: object, **_kwargs: object) -> None:
        await self._probe.external("storage")

    async def delete(self, *_args: object, **_kwargs: object) -> None:
        await self._probe.external("storage")

    async def exists(self, *_args: object, **_kwargs: object) -> bool:
        return True

    async def is_dir(self, *_args: object, **_kwargs: object) -> bool:
        return False


@pytest.fixture
def user() -> UserRecord:
    identity = IdentityRecord(id=uuid4())
    return UserRecord(
        id=uuid4(), tenant_id=uuid4(), role=UserRole.ORG_ADMIN, identity_id=identity.id, identity=identity
    )


@pytest.fixture
def probe(pool: CountingPool, user: UserRecord, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Probe:
    state = Probe(pool)

    async def current_user() -> UserRecord:
        return user

    async def tenant_lookup(tenant_id: UUID) -> TenantRecord:
        return TenantRecord(id=tenant_id, name="Contention tenant", slug="contention")

    async def authorize(*_args: object, **_kwargs: object) -> tuple[AgentRecord, str]:
        state.authority("access")
        return AgentRecord(id=uuid4(), name="agent", creator_id=user.id), "manage"

    async def fresh_authority(*_args: object, **_kwargs: object) -> UserRecord:
        state.authority("recheck")
        return user

    monkeypatch.setitem(main.app.dependency_overrides, get_current_user, current_user)
    monkeypatch.setattr(user_dao, "get_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(departments.tenant_dao, "get", tenant_lookup)
    monkeypatch.setattr(departments.org_department_dao, "list_for_tenant", AsyncMock(return_value=[]))
    monkeypatch.setattr(departments.org_department_dao, "list_assignments", AsyncMock(return_value=[]))
    monkeypatch.setattr(exclusion, "check_agent_access", authorize)
    monkeypatch.setattr(exclusion, "assert_no_active_run", AsyncMock())
    monkeypatch.setattr(exclusion, "assert_transfer_ready", AsyncMock())
    monkeypatch.setattr(exclusion.agent_manager, "_agent_dir", lambda agent_id: tmp_path / str(agent_id))
    monkeypatch.setattr(upload_api, "check_agent_access", authorize)
    monkeypatch.setattr(files_api, "check_agent_access", authorize)
    monkeypatch.setattr(files_api, "_fresh_agent_authority", AsyncMock(side_effect=_recheck(state, user)))
    monkeypatch.setattr(skills, "_fresh_import_actor", fresh_authority)
    for module in (upload_api, files_api):
        monkeypatch.setattr(module, "get_storage_backend", lambda: FakeStorage(state))
    return state


def _recheck(state: Probe, user: UserRecord) -> Callable[..., Awaitable[tuple[UserRecord, None, str]]]:
    async def recheck(*_args: object, **_kwargs: object) -> tuple[UserRecord, None, str]:
        state.authority("recheck")
        return user, None, "manage"

    return recheck


def _chat_upload(agent_id: UUID) -> Request:
    return lambda client: client.post(
        "/api/chat/upload", data={"agent_id": str(agent_id)}, files={"file": ("note.txt", b"hello", "text/plain")}
    )


def _workspace_upload(agent_id: UUID) -> Request:
    return lambda client: client.post(
        f"/api/agents/{agent_id}/files/upload", files={"file": ("note.txt", b"hello", "text/plain")}
    )


_KB = "/api/enterprise/knowledge-base"


def _probe_mcp(monkeypatch: pytest.MonkeyPatch, probe: Probe) -> Request:
    async def validate(url: str) -> str:
        return url

    async def list_tools(self: mcp_client.MCPClient) -> list[dict[str, str]]:
        await probe.external("mcp")
        return []

    monkeypatch.setattr(outbound_policy, "validate_public_url", validate)
    monkeypatch.setattr(mcp_client.MCPClient, "list_tools", list_tools)
    return lambda client: client.post("/api/tools/test-mcp", json={"server_url": "https://mcp.invalid/mcp"})


def _probe_email(monkeypatch: pytest.MonkeyPatch, probe: Probe) -> Request:
    async def connect(_config: object) -> dict[str, object]:
        await probe.external("mail")
        return {"ok": True}

    monkeypatch.setattr(email_service, "test_connection", connect)
    return lambda client: client.post("/api/tools/test-email", json={"config": {"email_address": "a@example.invalid"}})


def _probe_atlassian(monkeypatch: pytest.MonkeyPatch, probe: Probe) -> Request:
    async def check(agent_id: UUID, _user: UserRecord) -> dict[str, object]:
        await probe.external("atlassian")
        return {"ok": True, "message": "connected", "tool_count": 0, "tools": []}

    monkeypatch.setattr(atlassian, "check_atlassian_channel", check)
    return lambda client: client.post(f"/api/tools/agents/{uuid4()}/category-config/atlassian/test")


def _probe_agentbay(monkeypatch: pytest.MonkeyPatch, probe: Probe) -> Request:
    async def check(agent_id: UUID, _user: UserRecord) -> dict[str, object]:
        await probe.external("agentbay")
        return {"ok": True, "message": "connected"}

    monkeypatch.setattr(agentbay_client, "check_agentbay_channel", check)
    return lambda client: client.post(f"/api/tools/agents/{uuid4()}/category-config/agentbay/test")


@pytest.mark.parametrize(
    ("case", "authority"),
    [
        ("chat_upload", ["access"]),
        ("workspace_upload", ["access", "access", "recheck"]),
        ("kb_upload", ["recheck"]),
        ("kb_write", ["recheck"]),
        ("kb_delete", ["recheck"]),
        ("test_mcp", []),
        ("test_email", []),
        ("test_atlassian", []),
        ("test_agentbay", []),
    ],
)
async def test_blocked_external_io_holds_no_connection_and_leaves_the_slot_to_others(
    case: str, authority: list[str], probe: Probe, pool: CountingPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a single-slot pool and a request that blocks inside its external client
    agent_id = uuid4()
    requests: dict[str, Callable[[], Request]] = {
        "chat_upload": lambda: _chat_upload(agent_id),
        "workspace_upload": lambda: _workspace_upload(agent_id),
        "kb_upload": lambda: (
            lambda client: client.post(f"{_KB}/upload", files={"file": ("note.txt", b"hello", "text/plain")})
        ),
        "kb_write": lambda: (
            lambda client: client.put(f"{_KB}/content", params={"path": "a.txt"}, json={"content": "x"})
        ),
        "kb_delete": lambda: lambda client: client.delete(f"{_KB}/content", params={"path": "a.txt"}),
        "test_mcp": lambda: _probe_mcp(monkeypatch, probe),
        "test_email": lambda: _probe_email(monkeypatch, probe),
        "test_atlassian": lambda: _probe_atlassian(monkeypatch, probe),
        "test_agentbay": lambda: _probe_agentbay(monkeypatch, probe),
    }
    request = requests[case]()
    slow: list[httpx.Response] = []
    served: list[httpx.Response] = []
    finished = anyio.Event()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:

        async def run_slow() -> None:
            slow.append(await request(client))
            finished.set()

        # When another request needs the only slot while the external call is still pending
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(run_slow)
            try:
                with anyio.fail_after(3):
                    await probe.entered.wait()
                with anyio.fail_after(1):
                    served.append(await client.get("/api/departments"))
            finally:
                probe.release.set()
            with anyio.fail_after(3):
                await finished.wait()

    # Then authority ran in short scopes, the external call saw no connection, and the other request was served
    assert probe.events[:-1] == [(name, True, 1) for name in authority]
    assert probe.events[-1][1:] == (False, 0)
    assert slow[0].status_code == 200, slow[0].text
    assert served[0].status_code == 200
    assert pool.active == 0
    assert pool.checkouts == pool.releases
    assert pool.raw.commits == pool.checkouts
    assert pool.raw.rollbacks == 0


async def test_upload_policy_holds_a_connection_only_while_authenticating(
    pool: CountingPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the real dependency chain and a user lookup that must check out the pool
    identity = IdentityRecord(id=uuid4())
    user = UserRecord(id=uuid4(), tenant_id=uuid4(), identity_id=identity.id, identity=identity)
    seen: list[int] = []

    async def authenticate(_token: str, **_kwargs: object) -> UserRecord:
        seen.append(pool.active)
        return user

    monkeypatch.setattr("app.core.security.load_user_from_access_token", authenticate)

    # When
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.get("/api/chat/upload-policy", headers={"Authorization": "Bearer t"})

    # Then no request-wide checkout wraps the handler
    assert response.status_code == 200
    assert seen == [0]
    assert (pool.active, pool.checkouts) == (0, 0)
