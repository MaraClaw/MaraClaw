from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import ASGITransport, AsyncClient
from tests.tenant_email_fixtures import SMTPAdmin, smtp_admin

from app.core.domain.users import UserRole

__all__ = ["smtp_admin"]

from app.api import enterprise
from app.services import system_email_service

BASE = "/api/enterprise/system-settings/system_email_platform"


@pytest.mark.parametrize("role", ["member", "org_admin", "platform_admin"])
@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_retired_setting_is_unavailable_without_reading_or_writing_secrets(
    smtp_admin: SMTPAdmin, monkeypatch, role: str, method: str
):
    # Given a persisted retired credential and any authenticated role.
    smtp_admin.actor.role = UserRole(role)
    stored = {"SYSTEM_SMTP_PASSWORD": "retired-secret"}
    calls = [AsyncMock(return_value=SimpleNamespace(value=stored)), AsyncMock(), AsyncMock()]
    for name, call in zip(("get_by_key", "create", "update"), calls, strict=True):
        monkeypatch.setattr(enterprise.system_setting_dao, name, call)
    monkeypatch.setattr(enterprise, "optional_connection_ctx", lambda: nullcontext())
    # When either generic settings method targets the retired key.
    async with AsyncClient(transport=ASGITransport(app=smtp_admin.app), base_url="http://test") as client:
        response = await client.request(method, BASE, json={"value": {}} if method == "PUT" else None)
    # Then a tombstone blocks access before persistence, leaving old data untouched.
    assert response.status_code == 404
    assert "retired-secret" not in response.text
    assert stored == {"SYSTEM_SMTP_PASSWORD": "retired-secret"}
    for call in calls:
        call.assert_not_awaited()


@pytest.mark.parametrize("role", ["member", "org_admin", "platform_admin"])
async def test_retired_test_route_cannot_send_to_arbitrary_recipients(smtp_admin: SMTPAdmin, monkeypatch, role: str):
    # Given any authenticated role and an arbitrary destination.
    smtp_admin.actor.role = UserRole(role)
    send = Mock()
    monkeypatch.setattr(system_email_service, "_send_email_with_config_sync", send)
    # When the removed global SMTP test route is requested.
    async with AsyncClient(transport=ASGITransport(app=smtp_admin.app), base_url="http://test") as client:
        response = await client.post("/api/enterprise/system-email/test", json={"email": "victim@example.com"})
    # Then routing fails before any delivery can occur.
    assert response.status_code == 404
    send.assert_not_called()
