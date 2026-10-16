import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.transaction_pool_fakes import CountingPool

from app.api import google_workspace as api
from app.dao.user_dao import user_dao
from app.db import session as db_session
from app.records.identity import IdentityProviderRecord, IdentityRecord
from app.records.sso_scan_session import SSOScanSessionRecord
from app.records.user import UserRecord
from app.services.auth_provider import ExternalUserInfo, GoogleWorkspaceAuthProvider
from app.services.google_workspace_oauth import _sign_google_oauth_payload, parse_google_oauth_state


@dataclass
class GoogleFlow:
    pool: CountingPool = field(default_factory=CountingPool)
    provider: IdentityProviderRecord = field(
        default_factory=lambda: IdentityProviderRecord(
            uuid4(),
            "google_workspace",
            "Google",
            tenant_id=uuid4(),
            sso_login_enabled=True,
            config={"client_id": "client", "client_secret": "secret"},
        )
    )
    actor: UserRecord = field(default_factory=lambda: UserRecord(uuid4(), role="org_admin"))
    scan: SSOScanSessionRecord = field(
        default_factory=lambda: SSOScanSessionRecord(uuid4(), datetime.now(UTC) + timedelta(minutes=5))
    )
    committed: list[str] = field(default_factory=list)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    resume: asyncio.Event = field(default_factory=asyncio.Event)
    block_at: str = ""
    fail_at: str = ""

    def state(self, flow: str) -> str:
        if flow == "sync":
            return _sign_google_oauth_payload(f"google_sync:{self.provider.id}:{self.actor.id}:{self.actor.tenant_id}")
        return _sign_google_oauth_payload(f"google_sso:{self.scan.id}:{self.provider.id}")

    async def remote(self, step: str) -> None:
        assert db_session.get_connection() is None
        assert self.pool.active == 0
        if self.block_at == step:
            self.entered.set()
            await self.resume.wait()

    async def write(self, step: str) -> None:
        async with db_session.optional_connection_ctx():
            db_session.run_after_commit(lambda: self.committed.append(step))
            if self.fail_at == step:
                raise RuntimeError("injected write failure")
            if self.block_at == step:
                self.entered.set()
                await self.resume.wait()


@pytest.fixture
def google_flow(monkeypatch: pytest.MonkeyPatch) -> GoogleFlow:
    flow = GoogleFlow()
    flow.actor.tenant_id = flow.provider.tenant_id
    flow.actor.identity = IdentityRecord(uuid4())
    flow.scan.tenant_id = flow.provider.tenant_id
    monkeypatch.setattr(db_session, "get_pool", lambda: flow.pool)

    async def get_provider(_provider_id):
        return flow.provider

    async def get_actor(_user_id, *, fresh=False):
        assert fresh
        return flow.actor

    async def get_scan(_sid):
        return flow.scan

    async def redirect(_provider, _request=None):
        return "https://example.test/callback"

    async def exchange(_self, _code, redirect_uri=None):
        await flow.remote("exchange")
        return {"access_token": "access", "refresh_token": "refresh"}

    async def profile(_self, _access_token):
        await flow.remote("profile")
        return {"email": "admin@example.test"}

    async def user_info(_self, _access_token):
        await flow.remote("profile")
        return ExternalUserInfo(provider_type="google_workspace", provider_user_id="external")

    async def probe(_access_token, _customer_id):
        await flow.remote("probe")

    async def resolve(_self, _user_info, tenant_id=None):
        await flow.write("identity")
        await flow.write("membership")
        return flow.actor, True

    async def update_scan(*, db_obj, obj_in):
        await flow.write("scan")
        return db_obj

    async def update_provider(*, db_obj, obj_in):
        await flow.write("provider")
        return db_obj

    monkeypatch.setattr(api, "get_google_provider", get_provider)
    monkeypatch.setattr(api.identity_provider_dao, "get", get_provider)
    monkeypatch.setattr(user_dao, "get_with_identity", get_actor)
    monkeypatch.setattr(api.sso_scan_session_dao, "get", get_scan)
    monkeypatch.setattr(api.sso_scan_session_dao, "update", update_scan)
    monkeypatch.setattr(api.identity_provider_dao, "update", update_provider)
    monkeypatch.setattr(api, "get_google_redirect_uri", redirect)
    monkeypatch.setattr(api, "probe_google_directory", probe)
    monkeypatch.setattr(GoogleWorkspaceAuthProvider, "exchange_code_for_token", exchange)
    monkeypatch.setattr(GoogleWorkspaceAuthProvider, "fetch_openid_profile", profile)
    monkeypatch.setattr(GoogleWorkspaceAuthProvider, "get_user_info", user_info)
    monkeypatch.setattr(GoogleWorkspaceAuthProvider, "find_or_create_user", resolve)
    return flow


async def callback(flow: GoogleFlow, kind: str):
    app = FastAPI()
    app.include_router(api.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/auth/google_workspace/callback", params={"code": "code", "state": flow.state(kind)})


@pytest.mark.parametrize("failure", ["membership", "scan"])
async def test_sso_rolls_back_all_writes(google_flow: GoogleFlow, failure: str) -> None:
    google_flow.fail_at = failure

    response = await callback(google_flow, "sso")

    assert "Auth failed:" in response.text
    assert "Logged in" not in response.text
    assert google_flow.committed == []
    assert google_flow.pool.raw.rollbacks == 1
    assert google_flow.pool.active == 0


@pytest.mark.parametrize(
    ("kind", "step"), [("sso", "exchange"), ("sso", "profile"), ("sync", "exchange"), ("sync", "probe")]
)
async def test_remote_wait_releases_single_slot(google_flow: GoogleFlow, kind: str, step: str) -> None:
    google_flow.block_at = step
    task = asyncio.create_task(callback(google_flow, kind))
    try:
        await asyncio.wait_for(google_flow.entered.wait(), timeout=2)
        async with db_session.connection_ctx():
            assert google_flow.pool.active == 1
        google_flow.resume.set()
        response = await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert "successful" in response.text
    assert google_flow.pool.checkouts == google_flow.pool.releases
    assert google_flow.committed == (["provider"] if kind == "sync" else ["identity", "membership", "scan"])


@pytest.mark.parametrize(
    ("kind", "step"), [("sso", "exchange"), ("sso", "membership"), ("sso", "scan"), ("sync", "provider")]
)
async def test_cancellation_leaves_no_partial_writes(google_flow: GoogleFlow, kind: str, step: str) -> None:
    google_flow.block_at = step
    task = asyncio.create_task(callback(google_flow, kind))
    await asyncio.wait_for(google_flow.entered.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert google_flow.committed == []
    assert google_flow.pool.active == 0
    assert db_session.get_connection() is None


@pytest.mark.parametrize("change", ["actor", "identity", "password", "role", "tenant", "provider", "config"])
async def test_sync_rechecks_authority_after_exchange(google_flow: GoogleFlow, change: str) -> None:
    google_flow.block_at = "exchange"
    task = asyncio.create_task(callback(google_flow, "sync"))
    try:
        await asyncio.wait_for(google_flow.entered.wait(), timeout=2)
        match change:
            case "actor":
                google_flow.actor.is_active = False
            case "identity":
                assert google_flow.actor.identity is not None
                google_flow.actor.identity.is_active = False
            case "password":
                assert google_flow.actor.identity is not None
                google_flow.actor.identity.must_change_password = True
            case "role":
                google_flow.actor.role = "member"
            case "tenant":
                google_flow.actor.tenant_id = uuid4()
            case "provider":
                google_flow.provider.tenant_id = uuid4()
            case "config":
                google_flow.provider.config["client_secret"] = "rotated"
        google_flow.resume.set()
        response = await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert "authorization failed" in response.text
    assert google_flow.committed == []


async def test_old_sync_state_requires_restart(google_flow: GoogleFlow) -> None:
    app = FastAPI()
    app.include_router(api.router)
    state = _sign_google_oauth_payload(f"google_sync:{google_flow.provider.id}")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/auth/google_workspace/callback", params={"code": "code", "state": state})

    assert "restart" in response.text.lower()
    assert google_flow.committed == []


@pytest.mark.parametrize("change", ["scan_tenant", "scan_status", "scan_expiry", "provider"])
async def test_sso_reloads_scan_and_provider(google_flow: GoogleFlow, change: str) -> None:
    google_flow.block_at = "profile"
    task = asyncio.create_task(callback(google_flow, "sso"))
    try:
        await asyncio.wait_for(google_flow.entered.wait(), timeout=2)
        match change:
            case "scan_tenant":
                google_flow.scan.tenant_id = uuid4()
            case "scan_status":
                google_flow.scan.status = "authorized"
            case "scan_expiry":
                google_flow.scan.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            case "provider":
                google_flow.provider.is_active = False
        google_flow.resume.set()
        response = await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert "Auth failed:" in response.text
    assert google_flow.committed == []


async def test_authorize_url_binds_actor_and_tenant(google_flow: GoogleFlow) -> None:
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_admin] = lambda: google_flow.actor

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/enterprise/identity-providers/{google_flow.provider.id}/google-workspace-sync/authorize-url"
        )

    state = parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0]
    assert parse_google_oauth_state(state) == (
        "google_sync",
        (google_flow.provider.id, google_flow.actor.id, google_flow.actor.tenant_id),
    )
    assert parse_google_oauth_state(state.replace(str(google_flow.actor.id), str(uuid4()))) is None


async def test_provider_write_failure_rolls_back(google_flow: GoogleFlow) -> None:
    google_flow.fail_at = "provider"

    response = await callback(google_flow, "sync")

    assert "authorization failed" in response.text
    assert google_flow.committed == []
    assert google_flow.pool.raw.rollbacks == 1
