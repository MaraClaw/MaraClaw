from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
from fastapi import APIRouter
from tests.route_dispatch import effective_routes
from tests.transaction_pool_fakes import CountingPool

from app import main
from app.api import departments, enterprise, skills
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.dao.user_dao import user_dao
from app.db import session as session_module
from app.db.connection import Params
from app.db.pool import reset_pool_for_tests
from app.db.session import bind_crud_connection, get_connection
from app.records.identity import IdentityRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services.llm.types import LLMMessage, LLMResponse


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[CountingPool]:
    reset_pool_for_tests()
    token = session_module._conn_ctx.set(None)
    counting_pool = CountingPool()

    async def execute(query: str, params: Params = None) -> None:
        assert query in {
            "SELECT is_active FROM tenants WHERE id = %(id)s FOR UPDATE",
            "SELECT id FROM users WHERE id = %(id)s FOR UPDATE",
            "SELECT id FROM identities WHERE id = %(id)s FOR UPDATE",
        }
        assert params is not None

    @asynccontextmanager
    async def cursor() -> AsyncGenerator[Mock]:
        yield Mock(execute=execute, fetchone=AsyncMock(return_value={"is_active": True}))

    monkeypatch.setattr(counting_pool.raw, "cursor", cursor, raising=False)
    monkeypatch.setattr(session_module, "get_pool", lambda: counting_pool)
    try:
        yield counting_pool
    finally:
        session_module._conn_ctx.reset(token)
        reset_pool_for_tests()


@pytest.mark.parametrize(
    "router",
    [main.auth_router, main.files_router, main.enterprise_router, main.skills_router, main.google_workspace_router],
    ids=["auth", "files", "enterprise", "skills", "google_workspace"],
)
def test_io_router_has_no_request_wide_checkout(router: APIRouter) -> None:
    originals = {id(route) for route in router.routes}
    routes = [route for route in effective_routes(main.app) if id(route.context.original_route) in originals]

    assert routes
    bound = [
        route.path
        for route in routes
        if any(dependency.dependency is bind_crud_connection for dependency in route.context.dependencies)
    ]
    assert bound == []


@pytest.mark.parametrize(
    "router",
    [
        main.agents_router,
        main.departments_router,
        main.tasks_router,
        main.sso_router,
        main.org_router,
        main.advanced_router,
        main.upload_router,
        main.relationships_router,
        main.activity_router,
        main.messages_router,
        main.tenants_router,
        main.schedules_router,
        main.tools_router,
        main.files_upload_router,
        main.enterprise_kb_router,
        main.users_router,
        main.triggers_router,
        main.focus_router,
        main.chat_sessions_router,
        main.plaza_router,
        main.notification_router,
        main.admin_router,
        main.admin_linkup_router,
        main.admin_search_analytics_router,
        main.pages_router,
        main.credentials_router,
        main.gogcli_router,
        main.okr_router,
        main.onboarding_router,
    ],
)
def test_other_crud_routers_keep_request_binding(router: APIRouter) -> None:
    originals = {id(route) for route in router.routes}
    routes = [route for route in effective_routes(main.app) if id(route.context.original_route) in originals]

    assert routes
    assert all(
        sum(dependency.dependency is bind_crud_connection for dependency in route.context.dependencies) == 1
        for route in routes
    )


@pytest.mark.parametrize(
    "router",
    [
        main.feishu_router,
        main.slack_router,
        main.discord_router,
        main.dingtalk_router,
        main.google_chat_router,
        main.wecom_router,
        main.wechat_router,
        main.whatsapp_router,
        main.teams_router,
        main.atlassian_router,
        main.webhooks_router,
        main.ws_router,
        main.gateway_router,
        main.linkup_proxy_router,
        main.pages_public_router,
        main.agentbay_control_router,
    ],
)
def test_other_io_routers_remain_unbound(router: APIRouter) -> None:
    originals = {id(route) for route in router.routes}
    routes = [route for route in effective_routes(main.app) if id(route.context.original_route) in originals]

    assert routes
    assert all(
        dependency.dependency is not bind_crud_connection
        for route in routes
        for dependency in route.context.dependencies
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_path", ["/api/enterprise/llm-test", "/api/skills/clawhub/search?q=calendar"])
async def test_crud_progresses_while_remote_io_is_blocked(
    remote_path: str, pool: CountingPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = IdentityRecord(id=uuid4())
    user = UserRecord(
        id=uuid4(), tenant_id=uuid4(), role=UserRole.ORG_ADMIN, identity_id=identity.id, identity=identity
    )
    entered, release, finished = anyio.Event(), anyio.Event(), anyio.Event()
    remote_state: list[tuple[bool, int]] = []
    remote_responses: list[httpx.Response] = []

    async def current_user() -> UserRecord:
        return user

    async def tenant_lookup(tenant_id: UUID) -> TenantRecord:
        assert get_connection() is not None
        assert pool.active == 1
        return TenantRecord(id=tenant_id, name="Contention tenant", slug="contention")

    async def wait_remote() -> None:
        remote_state.append((get_connection() is None, pool.active))
        entered.set()
        with anyio.fail_after(5):
            await release.wait()

    async def complete(messages: list[LLMMessage], max_tokens: int) -> LLMResponse:
        await wait_remote()
        return LLMResponse(content="ok")

    async def fetch_clawhub(
        endpoint: str, *, api_key: str | None, params: dict[str, str]
    ) -> tuple[dict[str, list[dict[str, str]]], str]:
        await wait_remote()
        return {"results": []}, endpoint

    monkeypatch.setitem(main.app.dependency_overrides, get_current_user, current_user)
    monkeypatch.setattr(user_dao, "get_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(departments.tenant_dao, "get", tenant_lookup)
    monkeypatch.setattr(departments.org_department_dao, "list_for_tenant", AsyncMock(return_value=[]))
    monkeypatch.setattr(departments.org_department_dao, "list_assignments", AsyncMock(return_value=[]))
    monkeypatch.setattr(enterprise, "create_llm_client", Mock(return_value=Mock(complete=complete)))
    monkeypatch.setattr(skills, "_get_clawhub_key", AsyncMock(return_value="test-key"))
    monkeypatch.setattr(skills, "_fetch_clawhub_json", fetch_clawhub)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:

        async def request_remote() -> None:
            if remote_path == "/api/enterprise/llm-test":
                response = await client.post(
                    remote_path, json={"provider": "openai", "model": "test-model", "api_key": "test-key"}
                )
            else:
                response = await client.get(remote_path)
            remote_responses.append(response)
            finished.set()

        crud_response = None
        crud_timed_out = False
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(request_remote)
            try:
                with anyio.fail_after(3):
                    await entered.wait()
                with anyio.move_on_after(1) as crud_deadline:
                    crud_response = await client.get("/api/departments")
                crud_timed_out = crud_deadline.cancel_called
            finally:
                release.set()
            with anyio.fail_after(3):
                await finished.wait()

    assert remote_responses[0].status_code == 200
    if remote_path == "/api/enterprise/llm-test":
        assert remote_responses[0].json()["success"] is True
        assert remote_responses[0].json()["reply"] == "ok"
    else:
        assert remote_responses[0].json() == []
    assert pool.active == 0
    assert pool.checkouts == pool.releases
    assert pool.raw.commits == pool.checkouts
    assert pool.raw.rollbacks == 0
    assert not crud_timed_out, (
        f"CRUD waited for remote I/O to release the single pool slot; "
        f"remote (ambient_connection_absent, active_checkouts)={remote_state}"
    )
    assert remote_state == [(True, 0)]
    assert crud_response is not None
    assert crud_response.status_code == 200
    assert crud_response.json() == {"departments": [], "assignments": []}
