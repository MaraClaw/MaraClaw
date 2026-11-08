import time
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from redis.exceptions import RedisError

import user_roster_export_fixtures as fixtures
from app.core.domain.users import UserRole
from app.services import user_roster_tickets as tickets
from user_roster_export_fixtures import FOREIGN, TENANT, RosterDatabase

roster_db = fixtures.roster_db
roster_client = fixtures.roster_client


@pytest.fixture
def ticket_store(monkeypatch: pytest.MonkeyPatch) -> fixtures.TicketRedis:
    redis = fixtures.TicketRedis()
    monkeypatch.setattr(tickets, "get_redis", AsyncMock(return_value=redis))
    return redis


def binding() -> tickets.RosterTicketBinding:
    return tickets.RosterTicketBinding(
        user_id=UUID(int=1),
        identity_id=UUID(int=1),
        actor_tenant_id=TENANT,
        tenant_id=FOREIGN,
        search="%_\\",
        auth_deadline=int(time.time()) + 3600,
        auth_generation=7,
    )


async def test_ticket_is_hashed_expiring_single_use_and_contains_only_bound_context(ticket_store: fixtures.TicketRedis):
    context = binding()
    issued = await tickets.issue(context)
    assert issued.expires_in == 60
    assert list(ticket_store.values) == [tickets.ticket_key(issued.ticket)]
    value, expiry = next(iter(ticket_store.values.values()))
    assert issued.ticket not in value
    assert expiry == 60
    assert await tickets.redeem(issued.ticket) == context
    with pytest.raises(HTTPException) as error:
        await tickets.redeem(issued.ticket)
    assert error.value.status_code == 400


async def test_expired_and_malformed_tickets_are_consumed_and_refused(ticket_store: fixtures.TicketRedis):
    issued = await tickets.issue(binding())
    ticket_store.now = 61
    with pytest.raises(HTTPException) as error:
        await tickets.redeem(issued.ticket)
    assert error.value.status_code == 400
    ticket_store.values[tickets.ticket_key("forged")] = ("{}", 1000)
    with pytest.raises(HTTPException) as error:
        await tickets.redeem("forged")
    assert error.value.status_code == 400
    assert ticket_store.values == {}


@pytest.mark.parametrize("operation", ["issue", "redeem", "store"])
async def test_redis_failures_never_issue_or_authorize_a_fallback(
    ticket_store: fixtures.TicketRedis, monkeypatch: pytest.MonkeyPatch, operation: str
):
    if operation == "store":
        monkeypatch.setattr(ticket_store, "set", AsyncMock(return_value=False))
    else:
        monkeypatch.setattr(tickets, "get_redis", AsyncMock(side_effect=RedisError("unavailable")))
    with pytest.raises(HTTPException) as error:
        await (tickets.redeem("opaque") if operation == "redeem" else tickets.issue(binding()))
    assert error.value.status_code == 503


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("user_deleted", 401),
        ("user_inactive", 401),
        ("identity_inactive", 401),
        ("identity_missing", 401),
        ("identity_changed", 401),
        ("password_change", 403),
        ("generation", 401),
        ("member", 403),
        ("tenant", 403),
        ("company_deleted", 404),
        ("deadline", 401),
    ],
)
async def test_redemption_freshly_rejects_changed_auth_before_generation(
    roster_db: RosterDatabase, roster_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, change: str, code: int
):
    issued = await roster_client.post("/api/users/export", params={"search": "Admin"})
    handoff = tickets.UserRosterExportOut.model_validate_json(issued.content)
    match change:
        case "user_deleted":
            await roster_db.writer.execute("DELETE FROM users WHERE id=%(id)s", {"id": roster_db.actor.id})
        case "user_inactive":
            await roster_db.writer.execute("UPDATE users SET is_active=false")
        case "identity_inactive":
            await roster_db.writer.execute("UPDATE identities SET is_active=false")
        case "identity_missing":
            await roster_db.writer.execute("UPDATE users SET identity_id=NULL")
        case "identity_changed":
            await roster_db.writer.execute(
                "INSERT INTO identities (id, email) VALUES (%(id)s, 'new@example.com')", {"id": UUID(int=2)}
            )
            await roster_db.writer.execute("UPDATE users SET identity_id=%(id)s", {"id": UUID(int=2)})
        case "password_change":
            await roster_db.writer.execute("UPDATE identities SET must_change_password=true")
        case "generation":
            await roster_db.writer.execute("UPDATE identities SET auth_generation=auth_generation+1")
        case "member":
            await roster_db.writer.execute("UPDATE users SET role='member'")
        case "tenant":
            await roster_db.writer.execute("UPDATE users SET tenant_id=%(id)s", {"id": FOREIGN})
        case "company_deleted":
            monkeypatch.setattr(tickets.tenant_dao, "get", AsyncMock(return_value=None))
        case "deadline":
            monkeypatch.setattr(tickets.time, "time", lambda: 9999999999)
        case _:
            pytest.fail(f"unknown change {change}")
    response = await roster_client.get("/api/users/export/download", params={"ticket": handoff.ticket})
    assert response.status_code == code
    replay = await roster_client.get("/api/users/export/download", params={"ticket": handoff.ticket})
    assert replay.status_code == 400
    assert await roster_db.writer.fetchval("SELECT COUNT(*) FROM admin_audit_logs") == 0


@pytest.mark.parametrize("actor_tenant", [None, TENANT])
async def test_platform_admin_target_company_is_distinct_from_unchanged_actor_membership(
    roster_db: RosterDatabase, monkeypatch: pytest.MonkeyPatch, actor_tenant: UUID | None
):
    actor = replace(roster_db.actor, role=UserRole.PLATFORM_ADMIN, tenant_id=actor_tenant)
    principal = AsyncMock(return_value=actor)
    monkeypatch.setattr(tickets, "load_user_principal", principal)
    monkeypatch.setattr(tickets, "check_token_generation", AsyncMock())
    context = binding().model_copy(update={"actor_tenant_id": actor_tenant})
    assert await tickets.authorize(context) is actor
    principal.assert_awaited_once_with(actor.id, fresh=True)
