import smtplib
from contextlib import nullcontext
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from tests.tenant_email_fixtures import SMTPAdmin, smtp_admin

from app.core.domain.users import UserRole

__all__ = ["smtp_admin"]

from app.api import enterprise, notification
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.schemas.tenant_email import TenantEmailUpdate
from app.services import email_config, notification_service, system_email_service


async def test_invitation_preflight_and_background_delivery_use_same_organization(smtp_admin: SMTPAdmin, monkeypatch):
    # Given separate organization and platform transports and a real invitation route.
    smtp_admin.saved[smtp_admin.tenant_id] = email_config.stored_settings(
        TenantEmailUpdate(enabled=True, smtp_host="tenant.example.com", from_address="tenant@example.com"),
        {},
        public_only=True,
    )
    smtp_admin.configure_maraclaw()
    resolve = AsyncMock(wraps=email_config.resolve_email_config_async)
    send = Mock()
    monkeypatch.setattr(system_email_service, "resolve_email_config_async", resolve)
    monkeypatch.setattr(system_email_service, "_send_email_with_config_sync", send)
    monkeypatch.setattr(system_email_service, "render_email_template", AsyncMock(return_value=("Invitation", "Body")))
    monkeypatch.setattr(enterprise, "optional_connection_ctx", lambda: nullcontext())
    monkeypatch.setattr(enterprise, "run_after_commit", lambda callback: callback())
    monkeypatch.setattr(enterprise.invitation_code_dao, "create", AsyncMock())
    monkeypatch.setattr(
        enterprise.platform_service, "get_public_base_url", AsyncMock(return_value="https://app.example.com")
    )
    # When an invitation is submitted and its background task runs through HTTP.
    async with AsyncClient(transport=ASGITransport(app=smtp_admin.app), base_url="http://test") as client:
        result = await client.post("/api/enterprise/invite-users", json={"emails": ["invitee@example.com"]})
    # Then both preflight and actual delivery select only the requesting tenant.
    assert result.status_code == 200
    assert [call.args for call in resolve.await_args_list] == [(smtp_admin.tenant_id,), (smtp_admin.tenant_id,)]
    assert send.call_args.args[0].smtp_host == "tenant.example.com"
    assert send.call_args.args[1] == "invitee@example.com"


async def test_delete_stops_tenant_delivery_even_with_working_platform(smtp_admin: SMTPAdmin, monkeypatch):
    # Given usable tenant SMTP and a separate usable MaraClaw transport.
    smtp_admin.saved[smtp_admin.tenant_id] = email_config.stored_settings(
        TenantEmailUpdate(enabled=True, smtp_host="tenant.example.com", from_address="tenant@example.com"),
        {},
        public_only=True,
    )
    smtp_admin.configure_maraclaw()
    send = Mock()
    monkeypatch.setattr(system_email_service, "_send_email_with_config_sync", send)
    # When its genesis admin deletes the tenant configuration.
    async with AsyncClient(transport=ASGITransport(app=smtp_admin.app), base_url="http://test") as client:
        response = await client.delete("/api/enterprise/tenant-email")
    # Then subsequent tenant mail has no transport rather than a platform fallback.
    outcome = await system_email_service.attempt_email(
        "recipient@example.com", "Subject", "Body", message_id="<deleted@example.com>", tenant_id=smtp_admin.tenant_id
    )
    assert response.status_code == 204
    assert outcome.kind == "not_configured"
    send.assert_not_called()


async def test_delivery_failure_never_retries_with_platform(smtp_admin: SMTPAdmin, monkeypatch):
    # Given tenant delivery that fails after transport selection.
    smtp_admin.saved[smtp_admin.tenant_id] = email_config.stored_settings(
        TenantEmailUpdate(enabled=True, smtp_host="tenant.example.com", from_address="tenant@example.com"),
        {},
        public_only=True,
    )
    smtp_admin.configure_maraclaw()
    send = Mock(side_effect=smtplib.SMTPDataError(550, b"rejected"))
    monkeypatch.setattr(system_email_service, "_send_email_with_config_sync", send)
    # When the worker attempts this organization's email.
    outcome = await system_email_service.attempt_email(
        "recipient@example.com", "Subject", "Body", message_id="<failed@example.com>", tenant_id=smtp_admin.tenant_id
    )
    # Then the failure is classified without another SMTP attempt.
    assert outcome.kind == "permanent"
    assert send.call_count == 1
    assert send.call_args.args[0].smtp_host == "tenant.example.com"


@pytest.mark.parametrize("configured", [False, True])
async def test_platform_genesis_broadcast_requires_own_maraclaw_smtp(smtp_admin: SMTPAdmin, monkeypatch, configured):
    # Given a MaraClaw genesis PA and optionally configured organization SMTP.
    mara = next(row for row in smtp_admin.tenants.values() if row.slug == "maraclaw")
    smtp_admin.actor.tenant_id = mara.id
    smtp_admin.actor.role = UserRole.PLATFORM_ADMIN
    if configured:
        smtp_admin.configure_maraclaw()
    recipient = UserRecord(
        id=uuid4(), tenant_id=mara.id, identity=IdentityRecord(id=uuid4(), email="member@example.com")
    )
    enqueue = AsyncMock(return_value=1)
    notify = AsyncMock()
    monkeypatch.setattr(notification, "connection_ctx", lambda: nullcontext())
    monkeypatch.setattr(notification, "flush_request_transaction", AsyncMock())
    monkeypatch.setattr(notification.user_dao, "list_active_for_tenant", AsyncMock(return_value=[recipient]))
    monkeypatch.setattr(notification.agent_dao, "list_for_tenant", AsyncMock(return_value=[]))
    monkeypatch.setattr(notification_service, "send_notification", notify)
    monkeypatch.setattr(notification.broadcast_email_dao, "enqueue_many", enqueue)
    # When the platform genesis submits an email broadcast through HTTP.
    async with AsyncClient(transport=ASGITransport(app=smtp_admin.app), base_url="http://test") as client:
        response = await client.post(
            "/api/notifications/broadcast", json={"title": "News", "body": "Body", "send_email": True}
        )
    # Then a saved MaraClaw transport is required and the queue retains its tenant scope.
    assert response.status_code == (200 if configured else 400)
    if configured:
        assert enqueue.await_args is not None
        assert enqueue.await_args.args[0][0].tenant_id == mara.id
    else:
        enqueue.assert_not_awaited()
        notify.assert_not_awaited()
