from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI
from psycopg.errors import ForeignKeyViolation
from starlette.types import Message, Scope

from app.api import notification as api
from app.core.security import create_access_token, get_current_user
from app.dao.broadcast_email_dao import broadcast_email_dao as outbox
from app.db.session import bind_crud_connection
from app.records.user import UserRecord
from broadcast_email_persistence_fixtures import PersistenceDB, broadcast_db as broadcast_db, enqueue, member, tenant


def application(sender: UserRecord | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(api.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
    if sender is not None:
        app.dependency_overrides[get_current_user] = lambda: sender
    return app


@pytest.mark.parametrize("role", ["org_admin", "platform_admin"])
async def test_admin_status_and_cancellation_work_after_email_is_disabled(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    # Given: retained deliveries and disabled SMTP configuration.
    [scope] = await enqueue(broadcast_db)
    sender = await member(broadcast_db, scope.tenant_id)
    sender.role = role
    resolve = AsyncMock(return_value=None)
    monkeypatch.setattr("app.services.system_email_service.resolve_email_config_async", resolve)
    app = application(sender)
    # When: an administrator inspects and cancels the accepted queue.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        status = await client.get(f"/api/notifications/broadcast/{scope.broadcast_id}/email-status")
        cancelled = await client.post(f"/api/notifications/broadcast/{scope.broadcast_id}/cancel-email")
        replay = await client.post(f"/api/notifications/broadcast/{scope.broadcast_id}/cancel-email")
    app.dependency_overrides.clear()
    # Then: counts are public-only, scoped, post-operation and idempotent without config lookup.
    expected = {
        "broadcast_id": str(scope.broadcast_id),
        "available": True,
        "queued": 1,
        "claimed": 0,
        "smtp_accepted": 0,
        "failed": 0,
        "cancelled": 0,
    }
    assert status.status_code == cancelled.status_code == replay.status_code == 200
    assert status.json() == expected
    assert cancelled.json() == {**expected, "queued": 0, "cancelled": 1, "cancelled_now": 1}
    assert replay.json() == {**cancelled.json(), "cancelled_now": 0}
    resolve.assert_not_awaited()


@pytest.mark.parametrize("reason", ["missing", "foreign", "no_email", "no_recipient", "no_address", "retained_away"])
async def test_unavailable_broadcasts_have_the_same_wire_shape(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    # Given: no delivery evidence is available in the caller's current tenant.
    tenant_id = await tenant(broadcast_db)
    sender = await member(broadcast_db, tenant_id)
    sender.role = "platform_admin"
    broadcast_id = uuid4()
    monkeypatch.setattr(
        "app.services.system_email_service.resolve_email_config_async", AsyncMock(return_value=object())
    )
    if reason in {"foreign", "retained_away"}:
        [scope] = await enqueue(broadcast_db)
        broadcast_id = scope.broadcast_id
        if reason == "retained_away":
            sender.tenant_id = scope.tenant_id
            _ = await outbox.cancel_email(scope.tenant_id, scope.broadcast_id)
            assert await outbox.purge_settled(older_than=scope.created_at + timedelta(days=1), limit=1) == 1
    elif reason in {"no_email", "no_recipient", "no_address"}:
        if reason != "no_recipient":
            recipient = await member(broadcast_db, tenant_id)
            if reason == "no_address":
                await broadcast_db.admin.execute(
                    "UPDATE identities SET email = NULL WHERE id = %(id)s", {"id": recipient.identity_id}
                )
        receipt = await api.broadcast_notification(
            api.BroadcastRequest(title="Update", body="", send_email=reason != "no_email"), current_user=sender
        )
        broadcast_id = receipt["broadcast_id"]
        assert receipt["emails_sent"] == 0
    app = application(sender)
    # When: both aggregate operations target the unavailable identifier.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        status = await client.get(f"/api/notifications/broadcast/{broadcast_id}/email-status")
        cancelled = await client.post(f"/api/notifications/broadcast/{broadcast_id}/cancel-email")
    app.dependency_overrides.clear()
    # Then: foreign, absent, empty and expired history disclose no private distinction.
    expected = {
        "broadcast_id": str(broadcast_id),
        "available": False,
        "queued": 0,
        "claimed": 0,
        "smtp_accepted": 0,
        "failed": 0,
        "cancelled": 0,
    }
    assert status.status_code == cancelled.status_code == 200
    assert status.json() == expected
    assert cancelled.json() == {**expected, "cancelled_now": 0}


@pytest.mark.parametrize(
    ("condition", "expected"), [("anonymous", 401), ("member", 403), ("tenantless", 400), ("force_change", 403)]
)
async def test_management_endpoints_preserve_real_authentication_gates(
    broadcast_db: PersistenceDB, condition: str, expected: int
) -> None:
    # Given: actual PostgreSQL memberships, signed JWTs and the normal security dependency.
    [scope] = await enqueue(broadcast_db)
    sender = await member(broadcast_db, scope.tenant_id)
    await broadcast_db.admin.execute(
        "UPDATE users SET role = %(role)s, tenant_id = %(tenant)s WHERE id = %(id)s",
        {
            "id": sender.id,
            "role": "member" if condition == "member" else "org_admin",
            "tenant": None if condition == "tenantless" else scope.tenant_id,
        },
    )
    if condition == "force_change":
        await broadcast_db.admin.execute(
            "UPDATE identities SET must_change_password = true WHERE id = %(id)s", {"id": sender.identity_id}
        )
    headers: dict[str, str] = {}
    if condition != "anonymous":
        token = await create_access_token(str(sender.id), "org_admin", identity_id=sender.identity_id)
        headers["Authorization"] = f"Bearer {token}"
    # When: both routes are requested without overriding get_current_user.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(application()), base_url="http://test") as client:
        status = await client.get(f"/api/notifications/broadcast/{scope.broadcast_id}/email-status", headers=headers)
        cancelled = await client.post(
            f"/api/notifications/broadcast/{scope.broadcast_id}/cancel-email", headers=headers
        )
    # Then: neither route bypasses role, current tenant or first-login restrictions.
    assert status.status_code == cancelled.status_code == expected
    assert (await outbox.email_status(scope.tenant_id, scope.broadcast_id)).queued == 1


@pytest.mark.parametrize("commit_fails", [False, True])
async def test_cancellation_commits_before_response_and_keeps_in_app_notifications(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch, commit_fails: bool
) -> None:
    # Given: a production-bound request and an independent observer before response teardown.
    tenant_id = await tenant(broadcast_db)
    sender = await member(broadcast_db, tenant_id)
    sender.role = "org_admin"
    _ = await member(broadcast_db, tenant_id)
    monkeypatch.setattr(
        "app.services.system_email_service.resolve_email_config_async", AsyncMock(return_value=object())
    )
    receipt = await api.broadcast_notification(
        api.BroadcastRequest(title="Update", body="", send_email=True), current_user=sender
    )
    broadcast_id = receipt["broadcast_id"]
    assert isinstance(broadcast_id, UUID)
    if commit_fails:
        await broadcast_db.admin.execute("CREATE TABLE permitted_delivery_statuses (status text PRIMARY KEY)")
        await broadcast_db.admin.execute("INSERT INTO permitted_delivery_statuses VALUES ('pending')")
        await broadcast_db.admin.execute(
            "ALTER TABLE broadcast_emails ADD CONSTRAINT reject_cancellation_commit FOREIGN KEY (status) "
            "REFERENCES permitted_delivery_statuses(status) DEFERRABLE INITIALLY DEFERRED"
        )
    statuses: list[int] = []
    bodies: list[bytes] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        match message:
            case {"type": "http.response.start", "status": int(status)}:
                statuses.append(status)
            case {"type": "http.response.body", "body": bytes(body)}:
                bodies.append(body)
                assert await broadcast_db.admin.fetchone(
                    "SELECT (SELECT count(*) FROM notifications) AS notifications, "
                    "count(*) FILTER (WHERE status = 'cancelled') AS cancelled, "
                    "count(*) FILTER (WHERE status = 'pending') AS pending FROM broadcast_emails"
                ) == {"notifications": 1, "cancelled": 0 if commit_fails else 1, "pending": 1 if commit_fails else 0}

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/api/notifications/broadcast/{broadcast_id}/cancel-email",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "client": ("test", 123),
    }
    app = application(sender)
    # When: cancellation commits or its deferred PostgreSQL constraint rejects COMMIT.
    with pytest.raises(ForeignKeyViolation) if commit_fails else nullcontext():
        await app(scope, receive, send)
    app.dependency_overrides.clear()
    # Then: successful acknowledgement follows durability, and failed COMMIT produces HTTP 500.
    assert statuses == [500 if commit_fails else 200]
    assert len(bodies) == 1
