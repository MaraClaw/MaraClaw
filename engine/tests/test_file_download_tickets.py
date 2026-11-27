import hashlib
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from tests.membership_generation_fixtures import pin_membership_generation
from tests.rate_limit_fakes import install_limiter_redis

from app.api import files as files_api
from app.config import get_settings
from app.core import security
from app.core.rate_limit import TOO_MANY_REQUESTS_DETAIL
from app.core.security import create_access_token, decode_access_token, get_current_user
from app.services import file_download_tickets as tickets


class TicketRedis:
    def __init__(self):
        self.values = {}
        self.now = 0

    async def set(self, key, value, *, nx, ex):
        assert nx
        assert ex == 60
        if key in self.values:
            return False
        self.values[key] = (value, self.now + ex)
        return True

    async def getdel(self, key):
        value = self.values.pop(key, None)
        return value[0] if value and value[1] > self.now else None


class FileStorage:
    def __init__(self, keys):
        self.keys = set(keys)

    async def exists(self, key):
        return key in self.keys

    async def is_file(self, key):
        return key in self.keys


@pytest.fixture
def context(monkeypatch):
    redis = TicketRedis()
    user = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role="member",
        is_active=True,
        identity=SimpleNamespace(id=uuid.uuid4(), is_active=True, auth_generation=7),
    )
    agent_id = uuid.uuid4()
    binding = tickets.DownloadTicketBinding(
        user_id=user.id,
        tenant_id=user.tenant_id,
        agent_id=agent_id,
        path="workspace/report.pdf",
        inline=False,
        auth_deadline=int(time.time()) + 3600,
        auth_generation=7,
    )
    monkeypatch.setattr(tickets, "get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(tickets, "load_user_principal", AsyncMock(return_value=user))
    monkeypatch.setattr(tickets, "check_token_generation", AsyncMock())
    monkeypatch.setattr(tickets, "check_agent_access", AsyncMock(return_value=(SimpleNamespace(id=agent_id), "use")))
    return redis, binding, user


@pytest.fixture
async def mint(context, monkeypatch):
    _, binding, user = context
    storage = FileStorage({f"{binding.agent_id}/{binding.path}"})
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    access = AsyncMock(return_value=(SimpleNamespace(id=binding.agent_id), "use"))
    monkeypatch.setattr(files_api, "check_agent_access", access)
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=7))
    pin_membership_generation(monkeypatch)
    jwt = await create_access_token(str(user.id), user.role, identity_id=user.identity.id)

    async def call(path="workspace/report.pdf", inline=False, current_user=user):
        return await files_api.create_download_ticket(
            binding.agent_id,
            tickets.DownloadTicketRequest(path=path, inline=inline),
            Response(),
            current_user,
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=jwt),
        )

    return call, access, jwt


async def test_mint_stores_hashed_binding_without_session_jwt(context, mint):
    redis, binding, _ = context
    call, _, jwt = mint
    issued = await call(path="/workspace/report.pdf/", inline=True)
    assert issued.expires_in == 60
    digest = hashlib.sha256(issued.ticket.encode()).hexdigest()
    key = f"{get_settings().REDIS_KEY_PREFIX}:file-ticket:{digest}"
    assert list(redis.values) == [key]
    stored = redis.values[key][0]
    assert jwt not in stored
    assert issued.ticket not in stored
    assert jwt not in issued.model_dump_json()
    redeemed = await tickets.redeem(issued.ticket)
    assert redeemed == binding.model_copy(update={"inline": True, "auth_deadline": decode_access_token(jwt)["exp"]})


async def test_mint_requires_agent_access(mint):
    call, access, _ = mint
    access.side_effect = HTTPException(403, "denied")
    with pytest.raises(HTTPException) as exc:
        await call()
    assert exc.value.status_code == 403


async def test_mint_retains_admitted_jwt_generation_after_principal_changes(context, mint):
    # Given a principal refreshed after the presenting JWT was admitted.
    _, _, user = context
    call, _, jwt = mint
    user.identity.auth_generation += 1

    # When the download route mints a ticket from that JWT.
    issued = await call()

    # Then the ticket retains the JWT generation, not the refreshed principal value.
    binding = await tickets.redeem(issued.ticket)
    assert binding.auth_generation == decode_access_token(jwt)["gen"]
    assert binding.auth_generation != user.identity.auth_generation


async def test_mint_requires_existing_visible_file(context, mint):
    redis, _, _ = context
    call, _, _ = mint
    with pytest.raises(HTTPException) as exc:
        await call(path="workspace/missing.pdf")
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await call(path="../other-agent/secret.txt")
    assert exc.value.status_code == 404
    assert redis.values == {}


async def test_mint_requires_tenant(context, mint):
    redis, _, user = context
    call, _, _ = mint
    with pytest.raises(HTTPException) as exc:
        await call(current_user=SimpleNamespace(**{**vars(user), "tenant_id": None}))
    assert exc.value.status_code == 403
    assert redis.values == {}


async def test_ticket_is_single_use(context):
    _, binding, _ = context
    issued = await tickets.issue(binding)
    assert await tickets.redeem(issued.ticket) == binding
    with pytest.raises(HTTPException) as exc:
        await tickets.redeem(issued.ticket)
    assert exc.value.status_code == 400


async def test_expired_redis_key_and_bad_payload_are_invalid(context):
    redis, binding, _ = context
    issued = await tickets.issue(binding)
    redis.now = 61
    with pytest.raises(HTTPException) as exc:
        await tickets.redeem(issued.ticket)
    assert exc.value.status_code == 400
    redis.values[tickets.ticket_key("forged")] = ("{}", 1000)
    with pytest.raises(HTTPException) as exc:
        await tickets.redeem("forged")
    assert exc.value.status_code == 400


@pytest.mark.parametrize("operation", ["issue", "redeem"])
async def test_redis_failure_is_closed(context, monkeypatch, operation):
    _, binding, _ = context
    monkeypatch.setattr(tickets, "get_redis", AsyncMock(side_effect=RedisError()))
    with pytest.raises(HTTPException) as exc:
        await (tickets.issue(binding) if operation == "issue" else tickets.redeem("ticket"))
    assert exc.value.status_code == 503


async def test_unstored_ticket_is_unavailable(context, monkeypatch):
    redis, binding, _ = context
    monkeypatch.setattr(redis, "set", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await tickets.issue(binding)
    assert exc.value.status_code == 503


async def test_binding_matches_normalized_request(context, monkeypatch):
    _, binding, user = context
    principal = AsyncMock(return_value=user)
    access = AsyncMock(return_value=(SimpleNamespace(id=binding.agent_id), "use"))
    monkeypatch.setattr(tickets, "load_user_principal", principal)
    monkeypatch.setattr(tickets, "check_agent_access", access)
    authorized = await tickets.authorize_binding(
        binding, agent_id=binding.agent_id, path="/workspace/report.pdf/", inline=False
    )
    assert authorized is user
    access.assert_awaited_once_with(user, binding.agent_id, fresh=True)
    principal.assert_awaited_once_with(binding.user_id, fresh=True)


@pytest.mark.parametrize(
    "request_change",
    [{"agent_id": uuid.uuid4()}, {"path": "workspace/other.pdf"}, {"inline": True}],
)
async def test_binding_mismatch_is_refused(context, request_change):
    _, binding, _ = context
    request = {"agent_id": binding.agent_id, "path": binding.path, "inline": binding.inline, **request_change}
    with pytest.raises(HTTPException) as exc:
        await tickets.authorize_binding(binding, **request)
    assert (exc.value.status_code, exc.value.detail) == (403, "Ticket does not match this download")


async def test_session_deadline_is_enforced(context):
    _, binding, _ = context
    expired = binding.model_copy(update={"auth_deadline": int(time.time()) - 1})
    with pytest.raises(HTTPException) as exc:
        await tickets.authorize_binding(expired, agent_id=binding.agent_id, path=binding.path, inline=False)
    assert exc.value.status_code == 401


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("user_inactive", 401),
        ("identity_inactive", 401),
        ("password_change", 403),
        ("identity_missing", 401),
        ("tenant_changed", 403),
        ("access_revoked", 403),
    ],
)
async def test_access_revoked_between_mint_and_redeem(context, monkeypatch, change, code):
    _, binding, user = context
    match change:
        case "user_inactive":
            principal = AsyncMock(side_effect=HTTPException(401, "User not found or inactive"))
            monkeypatch.setattr(tickets, "load_user_principal", principal)
        case "identity_inactive":
            principal = AsyncMock(side_effect=HTTPException(401, "Identity is inactive"))
            monkeypatch.setattr(tickets, "load_user_principal", principal)
        case "password_change":
            principal = AsyncMock(side_effect=HTTPException(403, "Password change required"))
            monkeypatch.setattr(tickets, "load_user_principal", principal)
        case "identity_missing":
            user.identity = None
        case "tenant_changed":
            user.tenant_id = uuid.uuid4()
        case "access_revoked":
            monkeypatch.setattr(tickets, "check_agent_access", AsyncMock(side_effect=HTTPException(403, "denied")))
    with pytest.raises(HTTPException) as exc:
        await tickets.authorize_binding(binding, agent_id=binding.agent_id, path=binding.path, inline=False)
    assert exc.value.status_code == code


def _download_route(monkeypatch, jwt):
    """The real download-ticket route; the user it loads, the access check and the ticket it mints are counted."""
    loads = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4()))
    minted = AsyncMock(return_value=tickets.DownloadTicketResponse(ticket="opaque", expires_in=60))
    access = AsyncMock()
    monkeypatch.setattr(files_api, "check_agent_access", access)
    monkeypatch.setattr(files_api, "_downloadable_storage_key", AsyncMock())
    monkeypatch.setattr(tickets, "create", minted)
    monkeypatch.setattr(get_settings(), "TRUSTED_PROXIES", [])
    app = FastAPI()
    app.include_router(files_api.router, prefix="/api")

    async def current_user():
        return await loads()

    app.dependency_overrides[get_current_user] = current_user
    client = TestClient(app, client=("203.0.113.5", 1))
    url = f"/api/agents/{uuid.uuid4()}/files/download-ticket"
    return (
        client,
        lambda: client.post(url, json={"path": "workspace/a.txt"}, headers={"Authorization": f"Bearer {jwt}"}),
        loads,
        access,
        minted,
    )


async def test_download_ticket_throttle_refuses_a_spent_user_before_the_access_check(monkeypatch):
    # Given a budget of two tickets per user and the real route.
    install_limiter_redis(monkeypatch)
    monkeypatch.setattr(get_settings(), "TICKET_RATE_LIMIT_USER_ATTEMPTS", 2)
    pin_membership_generation(monkeypatch)
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=7))
    jwt = await create_access_token(str(uuid.uuid4()), "member", identity_id=uuid.uuid4())
    client, post, _, access, minted = _download_route(monkeypatch, jwt)

    with client:
        # When the user spends it, then the next request is a 429 before the access check or the mint.
        assert [post().status_code for _ in range(2)] == [200, 200]
        refused = post()
        assert refused.status_code == 429
        assert refused.json() == {"detail": TOO_MANY_REQUESTS_DETAIL}
        assert int(refused.headers["retry-after"]) > 0
        assert (access.await_count, minted.await_count) == (2, 2)


async def test_download_ticket_throttle_fails_open_when_redis_is_down(monkeypatch):
    redis = install_limiter_redis(monkeypatch)
    redis.broken_on_execute = True
    monkeypatch.setattr(get_settings(), "TICKET_RATE_LIMIT_USER_ATTEMPTS", 1)
    pin_membership_generation(monkeypatch)
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=7))
    jwt = await create_access_token(str(uuid.uuid4()), "member", identity_id=uuid.uuid4())
    client, post, _, _, minted = _download_route(monkeypatch, jwt)

    with client:
        assert [post().status_code for _ in range(3)] == [200] * 3
    assert minted.await_count == 3


async def test_download_ticket_throttle_refuses_a_spent_address_before_the_caller_is_loaded(monkeypatch):
    install_limiter_redis(monkeypatch)
    monkeypatch.setattr(get_settings(), "AUTH_RATE_LIMIT_ADDRESS_ATTEMPTS", 1)
    pin_membership_generation(monkeypatch)
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=7))
    jwt = await create_access_token(str(uuid.uuid4()), "member", identity_id=uuid.uuid4())
    client, post, loads, _, minted = _download_route(monkeypatch, jwt)

    with client:
        assert post().status_code == 200
        refused = post()

    assert (refused.status_code, refused.json()) == (429, {"detail": TOO_MANY_REQUESTS_DETAIL})
    assert (loads.await_count, minted.await_count) == (1, 1)


@pytest.fixture(autouse=True)
def limiter_redis(monkeypatch):
    return install_limiter_redis(monkeypatch)
