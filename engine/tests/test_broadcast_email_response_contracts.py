from collections.abc import AsyncIterator
from contextlib import nullcontext
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import TypeAdapter

from app.api import notification as api
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.records.agent import AgentRecord
from app.records.broadcast_history import BroadcastHistoryRecord
from app.records.broadcast_submission import BroadcastSubmissionClaim, BroadcastSubmissionRecord
from app.records.user import UserRecord
from app.schemas.notification_responses import BroadcastHistoryCursor, NotificationCursor


@pytest.fixture
def member() -> UserRecord:
    return UserRecord(id=uuid4(), identity_id=None, tenant_id=uuid4(), display_name="Member")


@pytest_asyncio.fixture
async def client(member: UserRecord) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: member
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as transport:
            yield transport
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(("role", "has_tenant", "status"), [("member", True, 403), ("org_admin", False, 400)])
async def test_broadcast_enforces_role_and_tenant(
    client: httpx.AsyncClient, member: UserRecord, role: str, has_tenant: bool, status: int
) -> None:
    # Given: an unauthorized membership or an administrator without a current tenant.
    member.role = UserRole(role)
    if not has_tenant:
        member.tenant_id = None
    # When: broadcast acceptance is requested.
    response = await client.post("/api/notifications/broadcast", json={"title": "Update"})
    # Then: existing role and tenant checks remain enforced.
    assert response.status_code == status


@pytest.fixture
def history(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    dao = AsyncMock()
    monkeypatch.setattr(api, "broadcast_history_dao", dao)
    return dao


async def test_broadcast_response_keeps_counts_and_tenant_scope(
    client: httpx.AsyncClient, member: UserRecord, history: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: one member and one agent receive an in-app-only broadcast.
    member.role = UserRole.ORG_ADMIN
    monkeypatch.setattr(api, "connection_ctx", nullcontext)
    users = AsyncMock(return_value=[member])
    agents = AsyncMock(return_value=[AgentRecord(id=uuid4(), name="Agent", creator_id=member.id)])
    sender = AsyncMock()
    monkeypatch.setattr(api.user_dao, "list_active_for_tenant", users)
    monkeypatch.setattr(api.agent_dao, "list_for_tenant", agents)
    monkeypatch.setattr("app.services.notification_service.send_notification", sender)
    # When: the real FastAPI response contract serializes acceptance.
    response = await client.post("/api/notifications/broadcast", json={"title": "Update"})
    # Then: the UUID is additive and existing accepted counts and tenant scope are unchanged.
    assert response.status_code == 200
    receipt = TypeAdapter(dict[str, object]).validate_json(response.content)
    assert TypeAdapter(UUID).validate_python(receipt.pop("broadcast_id"))
    assert receipt == {"ok": True, "users_notified": 1, "agents_notified": 1, "emails_sent": 0}
    users.assert_awaited_once_with(member.tenant_id, exclude_user_id=member.id, include_identity=False)
    agents.assert_awaited_once_with(member.tenant_id)
    assert sender.await_count == 2


def _receipt(member: UserRecord, request: api.BroadcastRequest) -> BroadcastSubmissionRecord:
    return BroadcastSubmissionRecord(
        id=uuid4(),
        tenant_id=member.tenant_id or uuid4(),
        sender_id=member.id,
        idempotency_key="draft-1",
        fingerprint=api._broadcast_fingerprint(request),
        broadcast_id=uuid4(),
        users_notified=7,
        agents_notified=3,
        emails_sent=5,
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def audience(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    users = AsyncMock(return_value=[])
    monkeypatch.setattr(api.user_dao, "list_active_for_tenant", users)
    monkeypatch.setattr(api.agent_dao, "list_for_tenant", AsyncMock(return_value=[]))
    return users


@pytest.fixture
def submissions(monkeypatch: pytest.MonkeyPatch, audience: AsyncMock, history: AsyncMock) -> AsyncMock:
    dao = AsyncMock()
    dao.get_for_key.return_value = None
    monkeypatch.setattr(api, "broadcast_submission_dao", dao)
    monkeypatch.setattr(api, "connection_ctx", nullcontext)
    monkeypatch.setattr(api, "flush_request_transaction", AsyncMock())
    return dao


@pytest.mark.parametrize("key", ["   ", "", "k" * 201])
async def test_blank_or_overlong_idempotency_key_is_a_validation_error(
    client: httpx.AsyncClient, member: UserRecord, submissions: AsyncMock, key: str
) -> None:
    member.role = UserRole.ORG_ADMIN
    response = await client.post(
        "/api/notifications/broadcast", json={"title": "Update"}, headers={"Idempotency-Key": key}
    )
    assert response.status_code == 422
    submissions.get_for_key.assert_not_awaited()


async def test_idempotency_key_does_not_bypass_role_or_tenant_gate(
    client: httpx.AsyncClient, member: UserRecord, submissions: AsyncMock
) -> None:
    response = await client.post(
        "/api/notifications/broadcast", json={"title": "Update"}, headers={"Idempotency-Key": "draft-1"}
    )
    assert response.status_code == 403
    submissions.get_for_key.assert_not_awaited()


async def test_replay_returns_stored_receipt_before_any_audience_lookup(
    client: httpx.AsyncClient, member: UserRecord, submissions: AsyncMock, audience: AsyncMock
) -> None:
    member.role = UserRole.ORG_ADMIN
    receipt = _receipt(member, api.BroadcastRequest(title="Update"))
    submissions.get_for_key.return_value = receipt
    response = await client.post(
        "/api/notifications/broadcast", json={"title": "Update"}, headers={"Idempotency-Key": "draft-1"}
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "broadcast_id": str(receipt.broadcast_id),
        "users_notified": 7,
        "agents_notified": 3,
        "emails_sent": 5,
    }
    submissions.get_for_key.assert_awaited_once_with(member.tenant_id, member.id, "draft-1")
    submissions.claim.assert_not_awaited()
    audience.assert_not_awaited()


@pytest.mark.parametrize("source", ["replay_read", "lost_claim"])
async def test_mismatched_payload_is_a_conflict_distinct_from_validation(
    client: httpx.AsyncClient, member: UserRecord, submissions: AsyncMock, audience: AsyncMock, source: str
) -> None:
    member.role = UserRole.ORG_ADMIN
    receipt = _receipt(member, api.BroadcastRequest(title="Original"))
    if source == "replay_read":
        submissions.get_for_key.return_value = receipt
    else:
        submissions.claim.return_value = BroadcastSubmissionClaim(receipt, created=False)
    response = await client.post(
        "/api/notifications/broadcast", json={"title": "Changed"}, headers={"Idempotency-Key": "draft-1"}
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "Idempotency-Key was already used for a different broadcast"}
    audience.assert_not_awaited()
    submissions.record_counts.assert_not_awaited()


async def test_first_keyed_request_claims_then_records_counts(
    client: httpx.AsyncClient,
    member: UserRecord,
    submissions: AsyncMock,
    audience: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member.role = UserRole.ORG_ADMIN
    receipt = _receipt(member, api.BroadcastRequest(title="Update"))
    submissions.claim.return_value = BroadcastSubmissionClaim(receipt, created=True)
    audience.return_value = [member]
    monkeypatch.setattr("app.services.notification_service.send_notification", AsyncMock())
    response = await client.post(
        "/api/notifications/broadcast", json={"title": "Update"}, headers={"Idempotency-Key": "draft-1"}
    )
    assert response.status_code == 200
    assert response.json()["users_notified"] == 1
    claim = submissions.claim.await_args
    assert claim is not None
    assert claim.kwargs["idempotency_key"] == "draft-1"
    assert claim.kwargs["fingerprint"] == receipt.fingerprint
    assert claim.kwargs["broadcast_id"] == UUID(response.json()["broadcast_id"])
    submissions.record_counts.assert_awaited_once_with(receipt.id, users=1, agents=0, emails=0)


async def test_missing_header_never_touches_receipts(
    client: httpx.AsyncClient, member: UserRecord, submissions: AsyncMock
) -> None:
    member.role = UserRole.ORG_ADMIN
    response = await client.post("/api/notifications/broadcast", json={"title": "Update"})
    assert response.status_code == 200
    assert submissions.method_calls == []


async def test_refused_email_broadcast_releases_its_claim(
    client: httpx.AsyncClient, member: UserRecord, submissions: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    member.role = UserRole.ORG_ADMIN
    receipt = _receipt(member, api.BroadcastRequest(title="Update", send_email=True))
    submissions.claim.return_value = BroadcastSubmissionClaim(receipt, created=True)
    monkeypatch.setattr("app.services.system_email_service.resolve_email_config_async", AsyncMock(return_value=None))
    response = await client.post(
        "/api/notifications/broadcast",
        json={"title": "Update", "send_email": True},
        headers={"Idempotency-Key": "draft-1"},
    )
    assert response.status_code == 400
    submissions.release.assert_awaited_once_with(receipt.id)
    submissions.record_counts.assert_not_awaited()


@pytest.mark.parametrize("key", [None, "draft-1"])
async def test_every_committed_broadcast_publishes_one_history_row(
    client: httpx.AsyncClient,
    member: UserRecord,
    submissions: AsyncMock,
    history: AsyncMock,
    audience: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    key: str | None,
) -> None:
    member.role = UserRole.ORG_ADMIN
    submissions.claim.return_value = BroadcastSubmissionClaim(
        _receipt(member, api.BroadcastRequest(title="Update", body="Body")), created=True
    )
    audience.return_value = [member]
    monkeypatch.setattr("app.services.notification_service.send_notification", AsyncMock())
    response = await client.post(
        "/api/notifications/broadcast",
        json={"title": "Update", "body": "Body"},
        headers={} if key is None else {"Idempotency-Key": key},
    )
    history.publish.assert_awaited_once()
    [entry] = history.publish.await_args.args
    assert (entry.broadcast_id, entry.tenant_id, entry.sender_id) == (
        UUID(response.json()["broadcast_id"]),
        member.tenant_id,
        member.id,
    )
    assert (entry.title, entry.body, entry.send_email) == ("Update", "Body", False)
    assert (entry.users_notified, entry.agents_notified, entry.emails_sent) == (1, 0, 0)


async def test_replay_conflict_and_refusal_publish_no_history(
    client: httpx.AsyncClient, member: UserRecord, submissions: AsyncMock, history: AsyncMock
) -> None:
    member.role = UserRole.ORG_ADMIN
    receipt = _receipt(member, api.BroadcastRequest(title="Update"))
    submissions.get_for_key.return_value = receipt
    replay = await client.post(
        "/api/notifications/broadcast", json={"title": "Update"}, headers={"Idempotency-Key": "draft-1"}
    )
    conflict = await client.post(
        "/api/notifications/broadcast", json={"title": "Other"}, headers={"Idempotency-Key": "draft-1"}
    )
    assert (replay.status_code, conflict.status_code) == (200, 409)
    history.publish.assert_not_awaited()


@pytest.mark.parametrize(("role", "has_tenant", "status"), [("member", True, 403), ("org_admin", False, 400)])
async def test_broadcast_history_enforces_role_and_tenant_before_reading(
    client: httpx.AsyncClient, member: UserRecord, history: AsyncMock, role: str, has_tenant: bool, status: int
) -> None:
    member.role = UserRole(role)
    if not has_tenant:
        member.tenant_id = None
    response = await client.get("/api/notifications/broadcast/history", params={"cursor": "not-a-cursor"})
    assert response.status_code == status
    history.list_page.assert_not_awaited()


def _history_record(member: UserRecord, minute: int) -> BroadcastHistoryRecord:
    return BroadcastHistoryRecord(
        broadcast_id=uuid4(),
        tenant_id=member.tenant_id or uuid4(),
        sender_id=member.id,
        sender_name="Member",
        title=f"Title {minute}",
        body="Body",
        send_email=True,
        users_notified=2,
        agents_notified=1,
        emails_sent=2,
        published_at=datetime(2026, 11, 6, 10, minute, tzinfo=UTC),
    )


async def test_broadcast_history_page_uses_caller_tenant_and_hides_lookahead_row(
    client: httpx.AsyncClient, member: UserRecord, history: AsyncMock
) -> None:
    member.role = UserRole.ORG_ADMIN
    records = [_history_record(member, minute) for minute in (3, 2, 1)]
    history.list_page.return_value = records
    response = await client.get("/api/notifications/broadcast/history", params={"limit": 2})
    assert response.status_code == 200
    page = response.json()
    assert [item["title"] for item in page["items"]] == ["Title 3", "Title 2"]
    assert set(page["items"][0]) == {
        "broadcast_id",
        "sender_id",
        "sender_name",
        "title",
        "body",
        "send_email",
        "published_at",
        "users_notified",
        "agents_notified",
        "emails_sent",
    }
    position = BroadcastHistoryCursor.decode(page["next_cursor"])
    assert (position.published_at, position.broadcast_id) == (records[1].published_at, records[1].broadcast_id)
    history.list_page.assert_awaited_once_with(member.tenant_id, limit=2, before=None)


async def test_broadcast_history_last_page_has_no_cursor_and_cursor_seeks(
    client: httpx.AsyncClient, member: UserRecord, history: AsyncMock
) -> None:
    member.role = UserRole.ORG_ADMIN
    record = _history_record(member, 1)
    history.list_page.return_value = [record]
    cursor = BroadcastHistoryCursor(version=1, published_at=record.published_at, broadcast_id=record.broadcast_id)
    response = await client.get("/api/notifications/broadcast/history", params={"cursor": cursor.encode()})
    assert response.json()["next_cursor"] is None
    history.list_page.assert_awaited_once_with(
        member.tenant_id, limit=20, before=(record.published_at, record.broadcast_id)
    )


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "!!!",
        "e30",
        NotificationCursor(version=1, created_at=datetime(2026, 11, 6, tzinfo=UTC), id=uuid4()).encode(),
        "a" * 1025,
    ],
)
async def test_broadcast_history_rejects_invalid_cursor(
    client: httpx.AsyncClient, member: UserRecord, history: AsyncMock, cursor: str
) -> None:
    member.role = UserRole.ORG_ADMIN
    response = await client.get("/api/notifications/broadcast/history", params={"cursor": cursor})
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid cursor"}
    history.list_page.assert_not_awaited()


@pytest.mark.parametrize("limit", [0, 101, -1])
async def test_broadcast_history_limit_is_bounded(
    client: httpx.AsyncClient, member: UserRecord, history: AsyncMock, limit: int
) -> None:
    member.role = UserRole.ORG_ADMIN
    response = await client.get("/api/notifications/broadcast/history", params={"limit": limit})
    assert response.status_code == 422
    history.list_page.assert_not_awaited()
