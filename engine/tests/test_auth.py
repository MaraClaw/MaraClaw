"""Unit tests for the authentication API (app/api/auth.py)."""

import asyncio
import threading
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Annotated, Any, override
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from jose import jwt
from starlette.requests import Request
from tests.membership_generation_fixtures import pin_membership_generation, recording_user_writes
from tests.oauth_state_fakes import install_state_redis
from tests.rate_limit_fakes import install_limiter_redis
from tests.test_redis_cache import FakeRedis

from app.api import auth as auth_api
from app.config import get_settings
from app.core import redis_cache, security, session_cache
from app.core.cache_contracts import SessionCacheObservation
from app.core.client_address import get_client_ip
from app.core.json_types import json_object_from_response, object_mapping_from
from app.core.row_memo import clear_row_memo
from app.core.security import create_access_token, hash_password
from app.dao.user_dao import _IDENTITY_COLUMNS, _USER_COLUMNS, user_dao
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.schemas.schemas import UserLogin
from app.services import chat_tickets, oauth_state, password_reset_service
from app.services.auth_throttle import throttle_sign_in

DEFAULT_TEST_CREDENTIAL = "correctpassword"


@pytest.mark.parametrize("flow", ["change", "reset"])
@pytest.mark.parametrize("forced", [False, True])
async def test_password_write_revokes_old_bearers_and_replaces_only_active_caller(
    monkeypatch: pytest.MonkeyPatch, flow: str, forced: bool
) -> None:
    # Given two admitted memberships sharing an identity, including a cached principal.
    identity = IdentityRecord(id=uuid.uuid4(), password_hash=hash_password("old-password"), must_change_password=forced)
    user = UserRecord(
        id=uuid.uuid4(), identity_id=identity.id, identity=identity, created_at=datetime(2026, 10, 25, tzinfo=UTC)
    )
    other = replace(user, id=uuid.uuid4())
    generation = [7]

    async def get_user(user_id: uuid.UUID, *, fresh: bool = False) -> UserRecord:
        return user if user_id == user.id else other

    async def get_generation(identity_id: uuid.UUID) -> int:
        assert identity_id == identity.id
        return generation[0]

    async def bump(identity_id: uuid.UUID) -> int:
        assert identity_id == identity.id
        generation[0] += 1
        return generation[0]

    async def update(*, db_obj: IdentityRecord, obj_in: dict[str, object]) -> IdentityRecord:
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    monkeypatch.setattr(security.user_dao, "get_with_identity", get_user)
    monkeypatch.setattr(security.identity_dao, "get", AsyncMock(return_value=identity))
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", get_generation)
    monkeypatch.setattr(security.identity_dao, "bump_auth_generation", bump)
    monkeypatch.setattr(security.identity_dao, "update", update)
    pin_membership_generation(monkeypatch)
    monkeypatch.setattr(
        password_reset_service, "consume_password_reset_token", AsyncMock(return_value={"identity_id": identity.id})
    )
    tokens = [
        await create_access_token(str(member.id), member.role, identity_id=identity.id) for member in (user, other)
    ]
    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api")

    @app.get("/privileged")
    async def privileged(current_user: Annotated[UserRecord, Depends(security.get_current_user)]) -> dict[str, str]:
        return {"id": str(current_user.id)}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {tokens[0]}"}
        assert (await client.get("/api/auth/me", headers=headers)).status_code == 200
        assert (await client.get("/privileged", headers=headers)).status_code == (403 if forced else 200)
        # When either password writer succeeds.
        response = await client.request(
            "PUT" if flow == "change" else "POST",
            "/api/auth/me/password" if flow == "change" else "/api/auth/reset-password",
            json={"old_password": "old-password", "new_password": "new-password", "token": "r" * 32},
            headers=headers if flow == "change" else {},
        )
        # Then neither old bearer can survive, even with the unchanged cached membership.
        assert response.status_code == 200
        for token in tokens:
            assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code == 401
        body = json_object_from_response(response)
        if flow == "reset":
            assert body == {"ok": True}
        else:
            assert body["ok"] is True
            assert body["must_change_password"] is False
            replacement = body["access_token"]
            assert isinstance(replacement, str)
            assert security.decode_access_token(replacement)["gen"] == 8
            headers = {"Authorization": f"Bearer {replacement}"}
            assert (await client.get("/api/auth/me", headers=headers)).status_code == 200
            assert (await client.get("/privileged", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_bearer_rejects_revoked_generation_with_cached_principal(monkeypatch):
    # Given a token shared by memberships of one identity and a cached principal.
    identity = IdentityRecord(id=uuid.uuid4())
    user = UserRecord(id=uuid.uuid4(), identity_id=identity.id, identity=identity)
    monkeypatch.setattr(security.user_dao, "get", AsyncMock(return_value=user))
    monkeypatch.setattr(security.user_dao, "get_with_identity", AsyncMock(return_value=user))
    generation = AsyncMock(return_value=7)
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", generation)
    pin_membership_generation(monkeypatch)
    token = await security.create_access_token(str(user.id), user.role)
    generation.return_value = 8

    # When the old credential is used, then fresh SQL wins over the cached user.
    with pytest.raises(HTTPException) as caught:
        await security.load_user_from_access_token(token)
    assert caught.value.status_code == 401
    assert generation.await_count == 2
    assert await security.load_user_principal(user.id) is user


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["membership", "identity", "generation", "membership_generation"])
async def test_mint_fails_closed_when_current_identity_cannot_be_resolved(monkeypatch, missing):
    # Given no valid persisted identity or membership generation for the membership.
    user = UserRecord(id=uuid.uuid4(), identity_id=None if missing == "identity" else uuid.uuid4())
    monkeypatch.setattr(security.user_dao, "get", AsyncMock(return_value=None if missing == "membership" else user))
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=None))
    pin_membership_generation(monkeypatch, None if missing == "membership_generation" else 0)

    # When minting, then no credential is issued from a snapshot or default generation.
    with pytest.raises(HTTPException) as caught:
        await security.create_access_token(str(user.id), user.role)
    assert caught.value.status_code == 401


@pytest.mark.asyncio
async def test_identity_only_mint_reads_persisted_generation_without_membership(monkeypatch):
    # Given an email-verification identity with no membership yet.
    identity_id = uuid.uuid4()
    membership = AsyncMock(side_effect=AssertionError("identity-only subject must not resolve membership"))
    monkeypatch.setattr(security.user_dao, "get", membership)
    generation = AsyncMock(return_value=9)
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", generation)
    no_membership_row = pin_membership_generation(monkeypatch, None)

    # When minting explicitly for that identity, then the persisted generation is retained.
    token = await security.create_access_token(str(identity_id), "user", identity_id=identity_id)
    assert security.decode_access_token(token)["gen"] == 9
    assert security.decode_access_token(token)["mgen"] == 0
    generation.assert_awaited_once_with(identity_id)
    no_membership_row.assert_awaited_once_with(identity_id)


@dataclass
class _Membership:
    """One membership row as the SQL writers leave it: only an active-to-inactive write advances the generation."""

    user: UserRecord
    generation: int = 0

    def deactivate(self) -> None:
        if self.user.is_active:
            self.user.is_active = False
            self.generation += 1

    def activate(self) -> None:
        self.user.is_active = True


class _MembershipDb:
    """The two reads the real UserDAO issues for a bearer: the joined principal and both generations."""

    def __init__(self, memberships: list[_Membership]) -> None:
        self.rows = {row.user.id: row for row in memberships}
        self.auth_generation = 5

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[_MembershipDb]:
        yield self

    async def fetchval(self, _sql: str, params: dict[str, uuid.UUID]) -> uuid.UUID | None:
        return self.rows[params["id"]].user.identity_id

    async def fetchone(self, sql: str, params: dict[str, uuid.UUID]) -> dict[str, object]:
        row = self.rows[params["user_id"]]
        if sql.startswith("SELECT i.auth_generation, u.membership_generation"):
            return {"auth_generation": self.auth_generation, "membership_generation": row.generation}
        if sql.startswith("SELECT membership_generation"):
            return {"membership_generation": row.generation}
        identity = row.user.identity
        assert identity is not None
        return {col: getattr(row.user, col) for col in _USER_COLUMNS} | {
            f"i_{col}": getattr(identity, col) for col in _IDENTITY_COLUMNS
        }


class _World:
    """Two memberships of one identity behind the real security module, UserDAO reads and session cache."""

    def __init__(self, memberships: list[_Membership], *, ttl: int, invalidate: bool) -> None:
        self.memberships = memberships
        self.ttl = ttl
        self.invalidate = invalidate

    @property
    def cache_can_hide_a_bump(self) -> bool:
        return self.ttl > 0 and not self.invalidate

    async def _written(self, membership: _Membership) -> None:
        if self.invalidate:
            await session_cache.bump_user_session(membership.user.id)

    async def deactivate(self, index: int = 0) -> None:
        self.memberships[index].deactivate()
        await self._written(self.memberships[index])

    async def activate(self, index: int = 0) -> None:
        self.memberships[index].activate()
        await self._written(self.memberships[index])

    async def mint(self, index: int = 0) -> str:
        clear_row_memo()
        return await security.create_access_token(str(self.memberships[index].user.id), "member")

    async def request(self, token: str, *, require_active: bool = True) -> UserRecord:
        clear_row_memo()
        return await security.load_user_from_access_token(token, require_active=require_active)


@pytest.fixture(
    params=[(0, True), (20, True), (20, False)], ids=["ttl-0", "cache-invalidated", "cache-misses-the-invalidation"]
)
def world(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    ttl, invalidate = request.param
    redis_cache.reset_circuit()
    clear_row_memo()
    redis = FakeRedis()

    async def get_cache_redis() -> FakeRedis:
        return redis

    monkeypatch.setattr(redis_cache, "_client", get_cache_redis)
    monkeypatch.setattr(session_cache, "get_settings", lambda: SimpleNamespace(USER_SESSION_CACHE_TTL_SECONDS=ttl))
    identity = IdentityRecord(id=uuid.uuid4(), email="member@example.com", email_verified=True)
    memberships = [
        _Membership(UserRecord(id=uuid.uuid4(), identity_id=identity.id, tenant_id=uuid.uuid4(), identity=identity))
        for _ in range(2)
    ]
    db = _MembershipDb(memberships)
    monkeypatch.setattr(user_dao, "session", db.session)
    monkeypatch.setattr(user_dao, "get", AsyncMock(side_effect=lambda user_id: db.rows[user_id].user))
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=5))
    yield _World(memberships, ttl=ttl, invalidate=invalidate)
    redis_cache.reset_circuit()
    clear_row_memo()


def _signed(**claims: object) -> str:
    body = {"role": "member", "exp": datetime.now(UTC) + timedelta(minutes=5), "gen": 5} | claims
    return str(jwt.encode(body, security.settings.JWT_SECRET_KEY, algorithm=security.settings.JWT_ALGORITHM))


@pytest.mark.parametrize("require_active", [True, False], ids=["active-only", "inactive-allowed"])
async def test_bearer_minted_before_a_deactivation_stays_dead_after_reactivation(
    world: _World, require_active: bool
) -> None:
    # Given a bearer that works and whose principal is cached when the cache is on.
    member = world.memberships[0].user
    old = await world.mint()
    assert (await world.request(old, require_active=require_active)).id == member.id

    # When the membership is deactivated, the cache possibly missing the invalidation.
    await world.deactivate()
    if world.cache_can_hide_a_bump:
        clear_row_memo()
        cached = await user_dao.get_with_identity(member.id)
        assert cached is not None
        assert cached.is_active is True

    # Then the old bearer is already rejected, whatever the cached principal says.
    with pytest.raises(HTTPException) as dead:
        await world.request(old, require_active=require_active)
    assert dead.value.status_code == 401

    # And reactivation does not bring it back, while a bearer issued afterwards works.
    await world.activate()
    with pytest.raises(HTTPException) as still_dead:
        await world.request(old, require_active=require_active)
    assert still_dead.value.status_code == 401
    fresh = await world.mint()
    assert (await world.request(fresh, require_active=require_active)).id == member.id


@pytest.mark.parametrize("require_active", [True, False], ids=["active-only", "inactive-allowed"])
async def test_bearer_without_a_membership_claim_works_until_the_first_deactivation(
    world: _World, require_active: bool
) -> None:
    # Given a bearer issued before membership generations existed.
    member = world.memberships[0].user
    legacy = _signed(sub=str(member.id))
    assert (await world.request(legacy, require_active=require_active)).id == member.id

    # When the membership is deactivated and reactivated, a missing claim never matches the revoked value.
    await world.deactivate()
    await world.activate()
    with pytest.raises(HTTPException) as caught:
        await world.request(legacy, require_active=require_active)
    assert caught.value.status_code == 401


async def test_activation_alone_never_revokes_a_bearer(world: _World) -> None:
    # Given a membership that has not been deactivated and a bearer issued for it.
    world.memberships[0].user.is_active = False
    await world.activate()
    token = await world.mint()

    # When the membership is activated again, the same bearer keeps working.
    await world.activate()
    assert (await world.request(token)).id == world.memberships[0].user.id


async def test_second_membership_of_the_same_identity_keeps_its_sessions(world: _World) -> None:
    # Given one bearer per membership of a shared identity.
    first, second = world.memberships[0].user, world.memberships[1].user
    first_token, second_token = await world.mint(0), await world.mint(1)

    # When only the first membership is deactivated and reactivated.
    await world.deactivate(0)
    await world.activate(0)

    # Then the first bearer is dead and the other membership is untouched.
    with pytest.raises(HTTPException) as caught:
        await world.request(first_token)
    assert caught.value.status_code == 401
    assert (await world.request(second_token)).id == second.id
    assert (await world.request(await world.mint(0))).id == first.id


async def test_minted_bearer_carries_the_membership_generation_it_was_issued_under(world: _World) -> None:
    # Given a membership that has been through a deactivation.
    await world.deactivate()
    await world.activate()
    membership = world.memberships[0]
    assert membership.generation != 0

    # When a bearer is minted, then the claim is the stored value, not a default.
    assert security.decode_access_token(await world.mint())["mgen"] == membership.generation


async def test_bearer_minted_for_an_explicit_identity_works_after_reactivation(world: _World) -> None:
    # Given a reactivated membership, as the password-change route sees it.
    await world.deactivate()
    await world.activate()
    member = world.memberships[0].user

    # When its replacement bearer is minted with the identity passed explicitly, then it is accepted.
    clear_row_memo()
    token = await security.create_access_token(str(member.id), member.role, identity_id=member.identity_id)
    assert (await world.request(token)).id == member.id


@pytest.mark.parametrize("claim", [None, True, False, -1, "0", 1.5, [], {}])
async def test_malformed_membership_claim_is_rejected_before_any_lookup(
    monkeypatch: pytest.MonkeyPatch, claim: object
) -> None:
    # Given a signed bearer whose membership claim is present but malformed, null included.
    lookup = AsyncMock(side_effect=AssertionError("a malformed claim must not reach any lookup"))
    monkeypatch.setattr(security.user_dao, "get_with_identity", lookup)

    # When it is presented, then it is rejected exactly like a malformed identity generation.
    with pytest.raises(HTTPException) as caught:
        await security.load_user_from_access_token(_signed(sub=str(uuid.uuid4()), mgen=claim))
    assert caught.value.status_code == 401
    lookup.assert_not_awaited()


@pytest.mark.parametrize(
    ("stored", "claimed", "accepted"),
    [(0, 0, True), (4, 4, True), (4, 0, False), (0, 4, False), (4, 5, False), (None, 0, False)],
    ids=["legacy", "current", "legacy-vs-revoked", "future", "stale", "membership-gone"],
)
async def test_membership_generation_is_compared_for_strict_equality(
    monkeypatch: pytest.MonkeyPatch, stored: int | None, claimed: int, accepted: bool
) -> None:
    # Given the stored generation of a membership, or none when the row has vanished.
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=7))
    pin_membership_generation(monkeypatch, stored)

    # When a claim is compared, only an exact match passes.
    if accepted:
        await security.check_generations(uuid.uuid4(), uuid.uuid4(), auth=7, membership=claimed)
        return
    with pytest.raises(HTTPException) as caught:
        await security.check_generations(uuid.uuid4(), uuid.uuid4(), auth=7, membership=claimed)
    assert caught.value.status_code == 401


@pytest.mark.parametrize(
    ("stored", "claimed", "accepted"),
    [(7, 7, True), (8, 7, False), (7, 8, False), (None, 7, False)],
    ids=["current", "revoked", "future", "identity-gone"],
)
async def test_token_generation_is_compared_for_strict_equality(
    monkeypatch: pytest.MonkeyPatch, stored: int | None, claimed: int, accepted: bool
) -> None:
    # Given the stored identity generation, or none when the identity has vanished.
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=stored))
    pin_membership_generation(monkeypatch, 0)

    # When a claim is compared, only an exact match passes.
    if accepted:
        await security.check_generations(uuid.uuid4(), uuid.uuid4(), auth=claimed, membership=0)
        return
    with pytest.raises(HTTPException) as caught:
        await security.check_generations(uuid.uuid4(), uuid.uuid4(), auth=claimed, membership=0)
    assert caught.value.status_code == 401


@pytest.mark.parametrize(
    ("auth", "membership", "accepted"),
    [(9, 4, True), (8, 4, False), (9, 3, False), (10, 5, False)],
    ids=["current", "identity-revoked", "membership-revoked", "both-ahead"],
)
async def test_both_generations_are_read_by_one_statement_and_compared_with_the_stored_values(
    monkeypatch: pytest.MonkeyPatch, auth: int, membership: int, accepted: bool
) -> None:
    # Given a membership whose stored counters are 9 (identity) and 4 (membership).
    user = UserRecord(id=uuid.uuid4(), identity_id=uuid.uuid4())
    assert user.identity_id is not None
    with recording_user_writes(monkeypatch, user) as raw:
        raw.auth_generation, raw.generation = 9, 4

        # When a bearer's claims are checked.
        if accepted:
            await security.check_generations(user.id, user.identity_id, auth=auth, membership=membership)
        else:
            with pytest.raises(HTTPException) as caught:
                await security.check_generations(user.id, user.identity_id, auth=auth, membership=membership)
            assert caught.value.status_code == 401

    # Then one statement answered both, bound to the membership and its identity.
    assert len(raw.executed) == 1
    query, params = raw.executed[0]
    assert "auth_generation" in query
    assert "membership_generation" in query
    assert params == {"user_id": user.id, "identity_id": user.identity_id}


async def test_a_session_cache_hit_request_costs_one_redis_round_trip_and_one_sql_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a warm session snapshot for a membership and its bearer.
    redis = FakeRedis()

    async def get_cache_redis() -> FakeRedis:
        return redis

    redis_cache.reset_circuit()
    clear_row_memo()
    monkeypatch.setattr(redis_cache, "_client", get_cache_redis)
    monkeypatch.setattr(session_cache, "_ttl", lambda: 20)
    identity = IdentityRecord(id=uuid.uuid4(), email="member@example.com", email_verified=True)
    user = UserRecord(id=uuid.uuid4(), identity_id=identity.id, tenant_id=uuid.uuid4(), identity=identity)
    user_version = await session_cache.peek_user_version(user.id)
    identity_version = await session_cache.peek_identity_version(identity.id)
    assert user_version is not None
    assert identity_version is not None
    await session_cache.set_cached_user(
        user, observed=SessionCacheObservation(identity.id, user_version, identity_version)
    )
    clear_row_memo()
    redis.commands.clear()
    redis.round_trips = 0

    # When the bearer authenticates a request.
    with recording_user_writes(monkeypatch, user) as raw:
        loaded = await security.load_user_from_access_token(_signed(sub=str(user.id), gen=0, mgen=0))

    # Then the principal came from the cache in one Redis call and both generations from one SQL statement.
    assert loaded.id == user.id
    assert redis.commands == ["pipeline"]
    assert redis.round_trips == 1
    assert len(raw.executed) == 1
    assert "auth_generation" in raw.executed[0][0]
    redis_cache.reset_circuit()
    clear_row_memo()


@pytest.mark.asyncio
async def test_chat_ticket_bearer_contract(monkeypatch):
    install_limiter_redis(monkeypatch)
    identity = IdentityRecord(id=uuid.uuid4())
    user = UserRecord(id=uuid.uuid4(), tenant_id=uuid.uuid4(), identity_id=identity.id, identity=identity)
    agent_id, session_id = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(security.user_dao, "get", AsyncMock(return_value=user))
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=7))
    pin_membership_generation(monkeypatch, 3)
    token = await create_access_token(str(user.id), "member")
    monkeypatch.setattr(auth_api, "load_user_from_access_token", AsyncMock(return_value=user))
    monkeypatch.setattr(chat_tickets, "authorize", AsyncMock())
    monkeypatch.setattr(
        chat_tickets, "check_agent_access", AsyncMock(return_value=(SimpleNamespace(is_expired=False), "use"))
    )
    monkeypatch.setattr(chat_tickets, "resolve_session", AsyncMock(return_value=session_id))
    issue = AsyncMock(return_value=chat_tickets.ChatTicketResponse(ticket="opaque", session_id=session_id))
    monkeypatch.setattr(chat_tickets, "issue", issue)
    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api")
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/chat-ticket", json={"agent_id": str(agent_id)}, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert response.json() == {"ticket": "opaque", "session_id": str(session_id)}
        assert response.headers["cache-control"] == "no-store"
        assert client.post("/api/auth/chat-ticket", json={"agent_id": str(agent_id)}).status_code in (401, 403)
    assert issue.await_args is not None
    binding = issue.await_args.args[0]
    assert binding.user_id == user.id
    assert binding.tenant_id == user.tenant_id
    assert binding.agent_id == agent_id
    assert binding.session_id == session_id
    assert binding.auth_deadline == auth_api.decode_access_token(token)["exp"]
    assert binding.auth_generation == 7
    assert binding.membership_generation == auth_api.decode_access_token(token)["mgen"] != 0


def _dummy_request() -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
        }
    )


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class DummyResult:
    def __init__(self, values=None, scalar_value=None):
        self._values = list(values or [])
        self._scalar_value = scalar_value

    def scalar_one_or_none(self):
        if self._values:
            return self._values[0]
        return self._scalar_value

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class RecordingDB:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.added = []
        self.committed = False
        self.refreshed = []

    async def execute(self, _statement, _params=None):
        if not self.responses:
            return DummyResult()
        return self.responses.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True

    async def refresh(self, value):
        self.refreshed.append(value)

    async def flush(self):
        pass


def _make_identity(
    *,
    email="test@example.com",
    password=DEFAULT_TEST_CREDENTIAL,
    is_active=True,
    email_verified=True,
):
    """Create a fake Identity object with hashed password."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        email=email,
        phone=None,
        password_hash=hash_password(password),
        is_active=is_active,
        email_verified=email_verified,
    )


def _make_user(identity_id, *, role="member", tenant_id=None):
    """Create a fake User object."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity_id,
        role=role,
        tenant_id=tenant_id or uuid.uuid4(),
        identity=_make_identity(),
        is_active=True,
    )


def _make_login_data(login_identifier="test@example.com", password=DEFAULT_TEST_CREDENTIAL):
    return UserLogin(
        login_identifier=login_identifier,
        password=password,
        tenant_id=None,
    )


# ---------------------------------------------------------------------------
# Login tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_invalid_credentials_no_identity(monkeypatch):
    """Login with a nonexistent user returns 401."""
    from app.dao import identity_dao

    async def no_identity(_identifier: str):
        return None

    monkeypatch.setattr(identity_dao, "get_by_login_identifier", no_identity)
    data = _make_login_data(login_identifier="nobody@example.com", password="whatever")
    bg = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await auth_api.login(data, bg, _dummy_request())
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_login_invalid_credentials_wrong_password(monkeypatch):
    """Login with wrong password returns 401."""
    from app.dao import identity_dao

    identity = _make_identity(password="correctpassword")

    async def get_identity(_identifier: str):
        return identity

    monkeypatch.setattr(identity_dao, "get_by_login_identifier", get_identity)
    data = _make_login_data(password="wrongpassword")
    bg = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await auth_api.login(data, bg, _dummy_request())
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_login_disabled_account(monkeypatch):
    """Login with a disabled account returns 403."""
    from app.dao import identity_dao

    identity = _make_identity(is_active=False)

    async def get_identity(_identifier: str):
        return identity

    monkeypatch.setattr(identity_dao, "get_by_login_identifier", get_identity)
    data = _make_login_data()
    bg = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await auth_api.login(data, bg, _dummy_request())
    assert exc.value.status_code == 403
    assert "disabled" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_login_unverified_email(monkeypatch):
    """Login with unverified email returns 403 with verification info."""
    from app.dao import identity_dao, user_dao

    identity = _make_identity(email_verified=False)
    user = _make_user(identity.id)

    async def get_identity(_identifier: str):
        return identity

    async def representative(_identity_id):
        return user

    monkeypatch.setattr(identity_dao, "get_by_login_identifier", get_identity)
    monkeypatch.setattr(user_dao, "get_representative_user_for_identity", representative)
    data = _make_login_data()
    bg = AsyncMock()

    with (
        patch(
            "app.services.system_email_service.resolve_email_config_async",
            new_callable=AsyncMock,
            return_value={"host": "localhost"},
        ),
        patch.object(auth_api, "_send_verification_email_task", new_callable=AsyncMock),
        pytest.raises(HTTPException) as exc,
    ):
        await auth_api.login(data, bg, _dummy_request())
    assert exc.value.status_code == 403
    detail = object_mapping_from(exc.value.detail)
    assert detail["needs_verification"] is True


# ---------------------------------------------------------------------------
# /me tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_me_returns_user():
    """GET /me with an authenticated user returns user data."""
    identity = SimpleNamespace(
        id=uuid.uuid4(),
        email="test@example.com",
        password_hash=hash_password(DEFAULT_TEST_CREDENTIAL),
    )
    user: Any = SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity.id,
        identity=identity,
        role="member",
        tenant_id=uuid.uuid4(),
        display_name="Test User",
        avatar_url=None,
        email=identity.email,
        is_platform_admin=False,
    )

    class DummyUserOut:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

        @classmethod
        def model_validate(cls, obj):
            return cls(id=str(obj.id), email=obj.email)

    with patch("app.api.auth.AuthUser", new=DummyUserOut):
        result = await auth_api.get_me(current_user=user)
    assert result.id == str(user.id)
    assert result.email == user.email
    assert result.is_platform_admin is False


@pytest.mark.asyncio
async def test_oauth_callback_exchanges_with_the_redirect_uri_stored_at_authorize(monkeypatch: pytest.MonkeyPatch):
    """The code exchange uses the redirect URI the server stored with the state, not the request body's."""
    install_state_redis(monkeypatch)
    state = await oauth_state.issue(
        "google", "https://example.com/oauth/callback/google", oauth_state.OAuthIntent.SIGN_IN
    )
    identity = _make_identity()
    user = _make_user(identity.id)
    provider = AsyncMock()
    provider.provider = None
    user.identity = identity
    user.tenant_id = None
    provider.exchange_code_for_token = AsyncMock(return_value={"access_token": "provider-token"})
    provider.get_user_info = AsyncMock(return_value=SimpleNamespace())
    provider.find_or_create_user = AsyncMock(return_value=(user, False))
    data: Any = SimpleNamespace(
        code="oauth-code",
        state=state,
        redirect_uri="https://evil.example/ignored",
        pending_token=None,
        tenant_id=None,
    )

    class DummyTokenResponse:
        def __init__(self, access_token, **kwargs):
            self.access_token = access_token

    with (
        patch("app.services.auth_registry.auth_provider_registry.get_provider", new=AsyncMock(return_value=provider)),
        patch("app.api.auth.TokenResponse", new=DummyTokenResponse),
        patch("app.api.auth.AuthUser") as mock_user_out,
        patch.object(auth_api, "create_access_token", return_value="jwt-token"),
        patch.object(auth_api.user_dao, "get_by_identity_id", new=AsyncMock(return_value=[user])),
        patch.object(auth_api.user_dao, "get_with_identity", new=AsyncMock(return_value=user)),
        patch.object(auth_api.identity_provider_dao, "get_preferred", new=AsyncMock(return_value=None)),
    ):
        mock_user_out.model_validate.return_value = {"id": str(user.id)}
        result = await auth_api.oauth_callback("google", data)

    provider.exchange_code_for_token.assert_awaited_once_with("oauth-code", "https://example.com/oauth/callback/google")
    assert isinstance(result, DummyTokenResponse)
    assert result.access_token == "jwt-token"


@pytest.mark.asyncio
async def test_register_init_returns_submitted_email(monkeypatch):
    """Canonical registration echoes submitted email even when user.email is missing."""
    from app.schemas.schemas import RegisterInitRequest

    # Given a fresh identity and a membership without a stored email.
    data = RegisterInitRequest(email="ada@example.com", password="secret1")
    identity = SimpleNamespace(id=uuid.uuid4(), email=None, email_verified=True)
    user = SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity.id,
        email=None,
        display_name="ada",
        avatar_url=None,
        role="member",
        is_platform_admin=False,
        tenant_id=None,
        title=None,
        primary_mobile=None,
        registration_source="web",
        is_active=True,
        email_verified=True,
        must_change_password=False,
        created_at=datetime.now(UTC),
        identity=None,
    )
    placement = SimpleNamespace(tenant_id=None, needs_org_confirm=False, suggested=None)

    @asynccontextmanager
    async def connection():
        yield

    monkeypatch.setattr(auth_api, "connection_ctx", connection)
    monkeypatch.setattr(auth_api.user_dao, "get_by_identity_id", AsyncMock(return_value=[]))
    monkeypatch.setattr(auth_api, "hash_password_async", AsyncMock(return_value="hashed"))
    monkeypatch.setattr(auth_api.identity_dao, "get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_api, "create_access_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "app.services.domain_admission_auto.stage_verified_domain_admission", AsyncMock(return_value=None)
    )

    with (
        patch("app.services.system_email_service.resolve_email_config_async", AsyncMock(return_value=None)),
        patch("app.services.org_membership.place_new_registration", AsyncMock(return_value=placement)),
        patch(
            "app.services.registration_service.registration_service.find_or_create_identity",
            AsyncMock(return_value=identity),
        ),
        patch(
            "app.services.registration_service.registration_service.create_user_with_identity",
            AsyncMock(return_value=user),
        ),
    ):
        # When registration runs through the current public handler.
        result = await auth_api.register_init(data, AsyncMock(), _dummy_request())

    # Then the response retains the submitted email and issued credentials.
    assert result.email == "ada@example.com"
    assert result.user_id == user.id
    assert result.access_token == "tok"
    assert "needs_company_setup" not in result.model_dump()


def test_http_header_reads_present_value_and_default():
    from app.core.json_types import http_header

    assert http_header({"content-type": "application/json"}, "content-type") == "application/json"
    assert http_header({}, "content-type", "image/png") == "image/png"


def _peer(host: str | None, forwarded: str | None = None) -> Request:
    headers = [] if forwarded is None else [(b"x-forwarded-for", forwarded.encode())]
    return Request(
        {"type": "http", "headers": headers, "client": None if host is None else (host, 4321), "method": "POST"}
    )


@pytest.mark.parametrize(
    ("trusted", "peer", "forwarded", "expected"),
    [
        ([], "203.0.113.9", "198.51.100.7", "203.0.113.9"),
        (["10.0.0.0/8"], "203.0.113.9", "198.51.100.7", "203.0.113.9"),
        (["10.0.0.0/8"], "10.1.2.3", "198.51.100.7", "198.51.100.7"),
        (["10.0.0.0/8"], "10.1.2.3", "192.0.2.1, 198.51.100.7", "198.51.100.7"),
        (["10.0.0.0/8"], "10.1.2.3", "192.0.2.1, 198.51.100.7, 10.9.9.9", "198.51.100.7"),
        (["10.0.0.0/8"], "10.1.2.3", "10.9.9.9", "10.9.9.9"),
        (["10.0.0.0/8"], "10.1.2.3", "198.51.100.7, garbage", "10.1.2.3"),
        (["10.0.0.0/8"], "10.1.2.3", None, "10.1.2.3"),
        (["10.1.2.3"], "10.1.2.3", "2001:db8::1", "2001:db8::1"),
        (["::1"], "::1", "198.51.100.7", "198.51.100.7"),
        (["10.0.0.0/8"], "testclient", "198.51.100.7", "testclient"),
    ],
    ids=[
        "no-trusted-proxy",
        "spoofed-header-from-untrusted-peer",
        "trusted-proxy",
        "client-prepended-fake-hop",
        "chain-through-a-second-proxy",
        "only-proxies-in-the-chain",
        "malformed-hop-stops-the-walk",
        "trusted-proxy-without-header",
        "exact-address-and-ipv6-client",
        "ipv6-proxy",
        "peer-that-is-not-an-address",
    ],
)
async def test_client_address_believes_forwarded_headers_only_from_trusted_proxies(
    monkeypatch: pytest.MonkeyPatch, trusted: list[str], peer: str, forwarded: str | None, expected: str
) -> None:
    monkeypatch.setattr(get_settings(), "TRUSTED_PROXIES", trusted)

    assert await get_client_ip(_peer(peer, forwarded)) == expected


async def test_client_address_is_none_without_a_peer() -> None:
    assert await get_client_ip(_peer(None)) is None


@pytest.mark.parametrize("entry", ["not-an-address", "10.0.0.0/33", "10.0.0.0/8, 10.0.0.1"])
def test_trusted_proxies_reject_entries_that_are_not_networks(entry: str) -> None:
    with pytest.raises(ValueError, match="TRUSTED_PROXIES"):
        type(get_settings()).model_validate({"TRUSTED_PROXIES": [entry]})


async def test_users_behind_a_trusted_proxy_get_separate_sign_in_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a proxy at 10.1.2.3 and a budget of one sign-in per address.
    settings = get_settings()
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.1.2.3"])
    monkeypatch.setattr(settings, "AUTH_RATE_LIMIT_ADDRESS_ATTEMPTS", 1)
    redis = install_limiter_redis(monkeypatch)

    # When two users and then a spoofing attacker connect through it, each user spends only their own budget.
    await throttle_sign_in(_peer("10.1.2.3", "198.51.100.1"))
    await throttle_sign_in(_peer("10.1.2.3", "198.51.100.2"))
    with pytest.raises(HTTPException) as repeated:
        await throttle_sign_in(_peer("10.1.2.3", "198.51.100.1"))
    assert repeated.value.status_code == 429
    assert sorted(key.split(":")[3] for key in redis.counts) == ["198.51.100.1", "198.51.100.2"]

    # And a direct client that lies about its address keeps the bucket of its real one.
    await throttle_sign_in(_peer("203.0.113.9", "198.51.100.1"))
    with pytest.raises(HTTPException):
        await throttle_sign_in(_peer("203.0.113.9", "198.51.100.77"))


async def test_one_shared_address_exhausts_only_its_own_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a budget of two sign-ins per address and nobody trusted: an office NAT behind one address.
    monkeypatch.setattr(get_settings(), "TRUSTED_PROXIES", [])
    monkeypatch.setattr(get_settings(), "AUTH_RATE_LIMIT_ADDRESS_ATTEMPTS", 2)
    install_limiter_redis(monkeypatch)

    # When the shared address spends it, then another address still signs in.
    await throttle_sign_in(_peer("203.0.113.9"))
    await throttle_sign_in(_peer("203.0.113.9"))
    with pytest.raises(HTTPException) as refused:
        await throttle_sign_in(_peer("203.0.113.9"))
    assert refused.value.status_code == 429
    await throttle_sign_in(_peer("203.0.113.10"))


@pytest.fixture
def bcrypt_gate(monkeypatch: pytest.MonkeyPatch) -> Iterator[threading.Event]:
    # Password work that blocks on a gate, so admitted checks stay queued or running until the test lets go.
    gate = threading.Event()

    def blocked(*_args: str) -> bool:
        assert gate.wait(timeout=10)
        return True

    monkeypatch.setattr(security.settings, "BCRYPT_MAX_PENDING_CHECKS", 2)
    monkeypatch.setattr(security, "verify_password", blocked)
    monkeypatch.setattr(security, "hash_password", lambda password: blocked(password) and "hash")
    yield gate
    gate.set()


@pytest.mark.parametrize("refused", ["verify", "hash"])
async def test_a_check_beyond_the_bcrypt_queue_bound_fails_fast_with_503(
    bcrypt_gate: threading.Event, refused: str
) -> None:
    # Given two admitted checks held on the executor, the configured bound.
    admitted = [asyncio.create_task(security.verify_password_async("pw", "hash")) for _ in range(2)]
    await asyncio.sleep(0)

    # When a third check arrives, then it is refused at once, without waiting for the executor.
    attempt = (
        security.verify_password_async("pw", "hash") if refused == "verify" else security.hash_password_async("pw")
    )
    with pytest.raises(HTTPException) as caught:
        await attempt
    assert caught.value.status_code == 503
    assert caught.value.headers is not None
    assert int(caught.value.headers["Retry-After"]) > 0

    # And once the admitted ones finish, the next check is admitted again.
    bcrypt_gate.set()
    assert await asyncio.gather(*admitted) == [True, True]
    assert await security.verify_password_async("pw", "hash") is True


class _RecordingExecutor(ThreadPoolExecutor):
    """Executor that lets a test await the end of work whose caller already gave up."""

    def __init__(self) -> None:
        super().__init__(max_workers=4, thread_name_prefix="bcrypt-test")
        self.finished: list[Callable[[], Awaitable[object]]] = []

    @override
    def submit[**P, T](self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        future = super().submit(fn, *args, **kwargs)
        self.finished.append(lambda: asyncio.wrap_future(future))
        return future


async def test_a_cancelled_caller_keeps_its_bcrypt_slot_until_the_check_finishes(
    bcrypt_gate: threading.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _RecordingExecutor()
    monkeypatch.setattr(security, "_bcrypt_executor", executor)
    # Given two admitted checks whose callers give up while the work is still on the executor.
    abandoned = [asyncio.create_task(security.verify_password_async("pw", "hash")) for _ in range(2)]
    await asyncio.sleep(0)
    for task in abandoned:
        _ = task.cancel()
    outcomes = await asyncio.gather(*abandoned, return_exceptions=True)
    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)

    # When another check arrives while that work still runs, then it is refused: the bound still counts it.
    with pytest.raises(HTTPException) as caught:
        await security.verify_password_async("pw", "hash")
    assert caught.value.status_code == 503

    # And once the abandoned work finishes, its slots return.
    bcrypt_gate.set()
    for finished in executor.finished:
        _ = await finished()
    assert await security.verify_password_async("pw", "hash") is True
    executor.shutdown()


async def test_a_failed_bcrypt_check_releases_its_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: str) -> bool:
        raise ValueError("bad salt")

    monkeypatch.setattr(security.settings, "BCRYPT_MAX_PENDING_CHECKS", 1)
    monkeypatch.setattr(security, "verify_password", broken)

    for _ in range(3):
        with pytest.raises(ValueError, match="bad salt"):
            await security.verify_password_async("pw", "hash")


async def test_password_hashes_and_stored_hashes_are_unchanged_by_the_queue() -> None:
    stored = hash_password("secret-password")

    assert stored.startswith("$2b$12$")
    assert await security.verify_password_async("secret-password", stored) is True
    assert await security.verify_password_async("other-password", stored) is False
    assert (await security.hash_password_async("secret-password")).startswith("$2b$12$")
