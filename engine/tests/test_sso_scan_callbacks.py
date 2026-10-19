from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import dingtalk, feishu, google_workspace, wecom
from app.core import security
from app.core.json_types import JsonObject
from app.dao.identity_provider_dao import identity_provider_dao
from app.dao.sso_scan_session_dao import sso_scan_session_dao
from app.dao.user_dao import user_dao
from app.db import session as db_session
from app.records.identity import IdentityProviderRecord, IdentityRecord
from app.records.sso_scan_session import SSOScanSessionRecord
from app.records.user import UserRecord
from app.services import sso_scan_sessions
from app.services.auth_provider import (
    PROVIDER_CLASSES,
    AuthProviderPayload,
    BaseAuthProvider,
    ExternalUserInfo,
    GoogleWorkspaceAuthProvider,
)
from app.services.auth_registry import auth_provider_registry
from app.services.google_workspace_oauth import sign_google_sso_state, sign_google_sync_state

PROVIDERS = ("dingtalk", "wecom", "feishu", "google_workspace")
SECRET = "provider-secret-sentinel"
JWT = "eyJhbGciOiJIUzI1NiJ9.callback-must-not-mint.signature"


@dataclass
class CallbackFlow:
    provider_type: str
    row: SSOScanSessionRecord
    user: UserRecord
    provider: IdentityProviderRecord
    lookups: list[str] = field(default_factory=list)
    exchanges: list[str] = field(default_factory=list)
    writes: list[SSOScanSessionRecord] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    minted: list[str] = field(default_factory=list)
    token_data: AuthProviderPayload = field(
        default_factory=lambda: {"access_token": SECRET, "refresh_token": SECRET, "id_token": SECRET}
    )
    raise_at: str = ""
    lose_authorization: bool = False
    user_info_calls: int = 0
    identity_present: bool = True

    def state(self, session_id: UUID | None = None) -> str:
        sid = session_id or self.row.id
        return sign_google_sso_state(sid, self.provider.id) if self.provider_type == "google_workspace" else str(sid)

    def fail_at(self, stage: str) -> None:
        if self.raise_at == stage:
            raise RuntimeError(SECRET)

    def error(self, message: str, *args: object) -> None:
        self.logs.append(f"{message} {args}")

    def exception(self, message: str, *args: object) -> None:
        self.error(message, *args)


@pytest.fixture
def flow(provider_type: str, monkeypatch: pytest.MonkeyPatch) -> CallbackFlow:
    row = SSOScanSessionRecord(uuid4(), datetime.now(UTC) + timedelta(minutes=5), tenant_id=uuid4())
    identity = IdentityRecord(uuid4())
    user = UserRecord(uuid4(), tenant_id=row.tenant_id, identity_id=identity.id, identity=identity)
    provider = IdentityProviderRecord(
        uuid4(),
        provider_type,
        provider_type,
        tenant_id=row.tenant_id,
        sso_login_enabled=True,
        config={"client_id": "client", "client_secret": SECRET},
    )
    result = CallbackFlow(provider_type, row, user, provider)

    async def get_scan(sid: UUID) -> SSOScanSessionRecord | None:
        result.fail_at("session")
        return replace(result.row) if sid == result.row.id else None

    async def authorize(
        sid: UUID, *, user_id: UUID, provider_type: str, tenant_id: UUID | None
    ) -> SSOScanSessionRecord | None:
        result.fail_at("authorize")
        if result.lose_authorization:
            result.row = replace(result.row, status="completed", user_id=uuid4())
        current = result.row
        if (
            sid != current.id
            or current.status not in ("pending", "scanned")
            or current.expires_at <= datetime.now(UTC)
            or (current.tenant_id is not None and current.tenant_id != tenant_id)
        ):
            return None
        result.row = replace(current, status="authorized", user_id=user_id, provider_type=provider_type)
        result.writes.append(replace(result.row))
        return replace(result.row)

    async def forbidden_update(*, db_obj: SSOScanSessionRecord, obj_in: object) -> SSOScanSessionRecord:
        pytest.fail("callbacks must use conditional authorization, not BaseDAO.update")

    async def get_provider_by_id(_provider_id: UUID) -> IdentityProviderRecord:
        result.lookups.append("provider_id")
        result.fail_at("lookup")
        return result.provider

    async def get_provider_by_type(_provider_type: str, _tenant_id: UUID | None) -> IdentityProviderRecord:
        result.lookups.append("provider_type")
        result.fail_at("lookup")
        return result.provider

    async def registry_provider(provider_type: str, tenant_id: str | None = None) -> BaseAuthProvider:
        result.lookups.append("registry")
        result.fail_at("lookup")
        assert tenant_id == (str(result.row.tenant_id) if result.row.tenant_id else None)
        return PROVIDER_CLASSES[provider_type](provider=result.provider)

    async def exchange(_self: BaseAuthProvider, code: str, redirect_uri: str | None = None) -> AuthProviderPayload:
        result.exchanges.append(code)
        result.fail_at("exchange")
        return result.token_data

    async def user_info(_self: BaseAuthProvider, _access_token: str) -> ExternalUserInfo:
        result.user_info_calls += 1
        result.fail_at("profile")
        return ExternalUserInfo(
            provider_type=provider_type,
            provider_user_id="external" if result.identity_present else None,
            provider_union_id="union" if result.identity_present else None,
            raw_data={"sensitive": SECRET},
        )

    async def resolve(
        _self: BaseAuthProvider, _info: ExternalUserInfo, tenant_id: str | None = None
    ) -> tuple[UserRecord, bool]:
        result.fail_at("resolve")
        assert tenant_id == (str(result.row.tenant_id) if result.row.tenant_id else None)
        return result.user, False

    async def fresh_user(_user_id: UUID, *, fresh: bool = False) -> UserRecord:
        assert fresh
        return result.user

    async def redirect(_provider: IdentityProviderRecord, _request: object = None) -> str:
        return "https://example.test/api/auth/google_workspace/callback"

    def mint(user_id: str, _role: str) -> str:
        result.minted.append(user_id)
        return JWT

    monkeypatch.setattr(sso_scan_session_dao, "get", get_scan)
    monkeypatch.setattr(sso_scan_session_dao, "authorize", authorize)
    monkeypatch.setattr(sso_scan_session_dao, "update", forbidden_update)
    monkeypatch.setattr(identity_provider_dao, "get", get_provider_by_id)
    monkeypatch.setattr(identity_provider_dao, "get_by_type_and_tenant", get_provider_by_type)
    monkeypatch.setattr(auth_provider_registry, "get_provider", registry_provider)
    monkeypatch.setattr(user_dao, "get_with_identity", fresh_user)
    monkeypatch.setattr(google_workspace, "get_google_redirect_uri", redirect)
    monkeypatch.setattr(security, "create_access_token", mint)
    monkeypatch.setattr(sso_scan_sessions, "create_access_token", mint)
    for module in (dingtalk, wecom, feishu, google_workspace):
        monkeypatch.setattr(module, "logger", result)
        if hasattr(module, "create_access_token"):
            monkeypatch.setattr(module, "create_access_token", mint)
    for provider_class in PROVIDER_CLASSES.values():
        monkeypatch.setattr(provider_class, "exchange_code_for_token", exchange)
        monkeypatch.setattr(provider_class, "get_user_info", user_info)
        monkeypatch.setattr(provider_class, "find_or_create_user", resolve)
    return result


@pytest.fixture
def client(flow: CallbackFlow, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # Given: real mounted routers and scan-session service with isolated I/O boundaries.
    from tests.transaction_pool_fakes import CountingPool

    pool = CountingPool()
    monkeypatch.setattr(db_session, "get_pool", lambda: pool)
    app = FastAPI()
    for module in (dingtalk, wecom, feishu, google_workspace):
        app.include_router(module.router, prefix="/api")
    with TestClient(app) as mounted:
        yield mounted
    assert pool.active == 0
    assert pool.checkouts == pool.releases
    assert flow.minted == []


def callback(client: TestClient, flow: CallbackFlow, state: str | None) -> tuple[int, str]:
    params = {"authCode" if flow.provider_type == "dingtalk" else "code": "authorization-code"}
    if state is not None:
        params["state"] = state
    response = client.get(f"/api/auth/{flow.provider_type}/callback", params=params)
    assert response.headers["content-type"].startswith("text/html")
    assert response.cookies == {}
    assert SECRET not in response.text
    assert JWT not in response.text
    assert all(SECRET not in entry and JWT not in entry for entry in flow.logs)
    return response.status_code, response.text


@pytest.mark.parametrize("provider_type", PROVIDERS)
@pytest.mark.parametrize("status", ["pending", "scanned"])
def test_callback_authorizes_without_jwt(client: TestClient, flow: CallbackFlow, status: str) -> None:
    # Given: a usable session with either authorizable status.
    flow.row.status = status
    # When: the real GET callback resolves the provider identity.
    status_code, html = callback(client, flow, flow.state())
    # Then: only user/provider authorization is stored and HTML contains only a typed session redirect.
    assert status_code == 200
    assert f"/sso/entry?sid={flow.row.id}&complete=1" in html
    assert flow.row.status == "authorized"
    assert flow.row.user_id == flow.user.id
    assert flow.row.provider_type == flow.provider_type
    assert flow.row.access_token is None
    assert flow.exchanges == ["authorization-code"]
    assert len(flow.writes) == 1


@pytest.mark.parametrize("provider_type", PROVIDERS)
@pytest.mark.parametrize(
    "condition",
    [
        "missing",
        "empty",
        "malformed",
        "unknown",
        "expired_pending",
        "expired_scanned",
        "authorized",
        "completed",
        "expired",
        "failed",
    ],
)
def test_invalid_state_stops_before_provider_lookup(client: TestClient, flow: CallbackFlow, condition: str) -> None:
    # Given: missing/untrusted state or a session that cannot be authorized.
    state = flow.state()
    match condition:
        case "missing":
            state = None
        case "empty":
            state = ""
        case "malformed":
            state = '<script>alert("state")</script>'
        case "unknown":
            state = flow.state(uuid4())
        case "expired_pending" | "expired_scanned":
            flow.row.status = condition.removeprefix("expired_")
            flow.row.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        case _:
            flow.row.status = condition
    before = replace(flow.row)
    # When: the callback receives the invalid state.
    status_code, html = callback(client, flow, state)
    # Then: it fails without looking up/exchanging a provider, reflecting state or changing the row.
    assert status_code == 200
    assert "failed" in html.lower()
    assert "<script>" not in html
    assert flow.lookups == []
    assert flow.exchanges == []
    assert flow.writes == []
    assert flow.row == before


@pytest.mark.parametrize("provider_type", PROVIDERS)
def test_callback_replay_stops_before_exchange(client: TestClient, flow: CallbackFlow) -> None:
    # Given: the browser has already authorized this session.
    callback(client, flow, flow.state())
    before = replace(flow.row)
    flow.lookups.clear()
    flow.exchanges.clear()
    flow.writes.clear()
    # When: the provider callback is replayed.
    _, html = callback(client, flow, flow.state())
    # Then: the second request cannot exchange the single-use code or change the row.
    assert "failed" in html.lower()
    assert "complete=1" not in html
    assert flow.lookups == []
    assert flow.exchanges == []
    assert flow.writes == []
    assert flow.row == before


@pytest.mark.parametrize("provider_type", PROVIDERS)
def test_cross_tenant_user_cannot_authorize(client: TestClient, flow: CallbackFlow) -> None:
    # Given: a provider identity resolves into another tenant.
    flow.user.tenant_id = uuid4()
    before = replace(flow.row)
    # When: the callback attempts authorization.
    _, html = callback(client, flow, flow.state())
    # Then: the conditional tenant boundary refuses it.
    assert "failed" in html.lower()
    assert flow.row == before
    assert flow.writes == []


@pytest.mark.parametrize("provider_type", PROVIDERS)
def test_authorization_race_loser_cannot_report_success(client: TestClient, flow: CallbackFlow) -> None:
    # Given: another callback consumes the session immediately before authorization.
    flow.lose_authorization = True
    # When: this callback loses the conditional transition.
    _, html = callback(client, flow, flow.state())
    # Then: it neither reports success nor overwrites the winner.
    assert "failed" in html.lower()
    assert "complete=1" not in html
    assert flow.row.status == "completed"
    assert flow.row.user_id != flow.user.id
    assert flow.row.access_token is None
    assert flow.writes == []


@pytest.mark.parametrize("provider_type", PROVIDERS)
@pytest.mark.parametrize("stage", ["session", "lookup", "exchange", "profile", "resolve", "authorize"])
def test_callback_redacts_exceptions(client: TestClient, flow: CallbackFlow, stage: str) -> None:
    # Given: a boundary exception includes a provider secret.
    flow.raise_at = stage
    # When: the callback handles the exception.
    status_code, html = callback(client, flow, flow.state())
    # Then: logs and HTML contain only a static error, with no authorization.
    assert status_code == 200
    assert "failed" in html.lower()
    assert flow.logs
    assert flow.writes == []


@pytest.mark.parametrize("provider_type", PROVIDERS)
@pytest.mark.parametrize("access_token", [None, "", 42])
def test_callback_redacts_invalid_token_payload(
    client: TestClient, flow: CallbackFlow, access_token: str | int | None
) -> None:
    # Given: an invalid token response with sensitive refresh/id/error fields.
    flow.token_data = {"access_token": access_token, "refresh_token": SECRET, "id_token": SECRET, "error": SECRET}
    # When: the callback receives the provider response.
    _, html = callback(client, flow, flow.state())
    # Then: it never fetches a profile or authorizes a session, and no payload is exposed.
    assert "failed" in html.lower()
    assert flow.user_info_calls == 0
    assert flow.writes == []


@pytest.mark.parametrize("provider_type", ["dingtalk", "wecom"])
def test_callback_redacts_missing_provider_identity(client: TestClient, flow: CallbackFlow) -> None:
    # Given: user info has no required union/user id but includes a sensitive raw payload.
    flow.identity_present = False
    # When: the callback receives user info.
    _, html = callback(client, flow, flow.state())
    # Then: the invalid identity is rejected without logging raw_data.
    assert "failed" in html.lower()
    assert flow.writes == []


@pytest.mark.parametrize("provider_type", ["google_workspace"])
def test_google_legacy_uuid_state_still_authorizes(
    client: TestClient, flow: CallbackFlow, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: an already-shipped plain UUID scan state.
    async def preferred(_provider_type: str, _tenant_id: str | None = None) -> IdentityProviderRecord:
        return flow.provider

    monkeypatch.setattr(google_workspace, "get_preferred_identity_provider", preferred)
    # When: the Google callback receives the legacy UUID.
    _, html = callback(client, flow, str(flow.row.id))
    # Then: it authorizes through the same token-free transition.
    assert "complete=1" in html
    assert flow.row.status == "authorized"
    assert flow.row.access_token is None


@pytest.mark.parametrize("provider_type", ["google_workspace"])
def test_google_tampered_signed_state_stops_before_lookup(client: TestClient, flow: CallbackFlow) -> None:
    # Given: a signed state with an altered provider binding.
    state = flow.state().replace(str(flow.provider.id), str(uuid4()))
    # When: Google receives the tampered state.
    _, html = callback(client, flow, state)
    # Then: neither a provider lookup nor exchange occurs.
    assert "failed" in html.lower()
    assert flow.lookups == []
    assert flow.exchanges == []


@pytest.mark.parametrize("provider_type", ["google_workspace"])
def test_google_admin_sync_does_not_require_scan_session(
    client: TestClient, flow: CallbackFlow, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: valid signed admin authorization, independently of a completed scan session.
    flow.row.status = "completed"
    flow.user.role = "org_admin"
    flow.provider.config["client_secret"] = "configured-client-secret"
    updates: list[JsonObject] = []

    async def profile(_self: GoogleWorkspaceAuthProvider, _access_token: str) -> AuthProviderPayload:
        return {"email": "admin@example.test"}

    async def probe(_access_token: str, _customer_id: str) -> None:
        return None

    async def update_provider(*, db_obj: IdentityProviderRecord, obj_in: JsonObject) -> IdentityProviderRecord:
        updates.append(obj_in)
        return db_obj

    monkeypatch.setattr(GoogleWorkspaceAuthProvider, "fetch_openid_profile", profile)
    monkeypatch.setattr(google_workspace, "probe_google_directory", probe)
    monkeypatch.setattr(identity_provider_dao, "update", update_provider)
    state = sign_google_sync_state(flow.provider.id, flow.user.id, flow.user.tenant_id)
    # When: the unified mounted Google callback receives admin state.
    _, html = callback(client, flow, state)
    # Then: admin credentials are saved without touching or redeeming the scan session.
    assert "google-workspace-sync-authorized" in html
    assert len(updates) == 1
    assert SECRET not in str(updates)
    config = updates[0]["config"]
    assert isinstance(config, dict)
    encrypted = config["google_admin_refresh_token_encrypted"]
    assert isinstance(encrypted, str)
    assert security.decrypt_data(encrypted, google_workspace.settings.SECRET_KEY) == SECRET
    assert flow.row.status == "completed"
    assert flow.writes == []
