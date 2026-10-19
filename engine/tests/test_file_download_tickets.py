import hashlib
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials
from redis.exceptions import RedisError

from app.api import files as files_api
from app.config import get_settings
from app.core.security import create_access_token, decode_access_token
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
        identity=SimpleNamespace(is_active=True),
    )
    agent_id = uuid.uuid4()
    binding = tickets.DownloadTicketBinding(
        user_id=user.id,
        tenant_id=user.tenant_id,
        agent_id=agent_id,
        path="workspace/report.pdf",
        inline=False,
        auth_deadline=int(time.time()) + 3600,
    )
    monkeypatch.setattr(tickets, "get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(tickets, "load_user_principal", AsyncMock(return_value=user))
    monkeypatch.setattr(tickets, "check_agent_access", AsyncMock(return_value=(SimpleNamespace(id=agent_id), "use")))
    return redis, binding, user


@pytest.fixture
def mint(context, monkeypatch):
    _, binding, user = context
    storage = FileStorage({f"{binding.agent_id}/{binding.path}"})
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    access = AsyncMock(return_value=(SimpleNamespace(id=binding.agent_id), "use"))
    monkeypatch.setattr(files_api, "check_agent_access", access)
    jwt = create_access_token(str(user.id), user.role)

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
