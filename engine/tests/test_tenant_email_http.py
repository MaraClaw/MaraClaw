from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from tests.tenant_email_fixtures import SMTPAdmin, smtp_admin

from app.core.domain.users import UserRole

__all__ = ["smtp_admin"]

from app.schemas.tenant_email import TenantEmailUpdate
from app.services import email_config, system_email_service, tenant_email_admin

BASE = "/api/enterprise/tenant-email"
COMPLETE = {"enabled": True, "smtp_host": "smtp.example.com", "from_address": "org@example.com"}


@pytest.mark.parametrize(("method", "suffix"), [("GET", ""), ("PUT", ""), ("DELETE", ""), ("POST", "/test")])
@pytest.mark.parametrize(
    "condition", ["member", "foreign", "foreign_pa", "additional_oa", "additional_pa", "force_change"]
)
async def test_rbac_rejects_forbidden_requests(smtp_admin: SMTPAdmin, method: str, suffix: str, condition: str):
    # Given a forbidden principal or target selection.
    query = ""
    if condition == "member":
        smtp_admin.actor.role = UserRole.MEMBER
    elif condition in ("foreign", "foreign_pa"):
        query = f"?tenant_id={uuid4()}"
        if condition == "foreign_pa":
            smtp_admin.actor.role = UserRole.PLATFORM_ADMIN
    elif condition in ("additional_oa", "additional_pa"):
        smtp_admin.actor.is_genesis = False
        smtp_admin.actor.role = UserRole.PLATFORM_ADMIN if condition == "additional_pa" else UserRole.ORG_ADMIN
    else:
        assert smtp_admin.actor.identity is not None
        smtp_admin.actor.identity.must_change_password = True
    # When the public HTTP endpoint is called.
    async with AsyncClient(transport=ASGITransport(app=smtp_admin.app), base_url="http://test") as client:
        result = await client.request(method, BASE + suffix + query, json={} if method == "PUT" else None)
    # Then no configuration is written or test audited.
    assert result.status_code == 403
    assert smtp_admin.saved == {}
    assert smtp_admin.audits == []


async def test_get_returns_exact_empty_draft_contract(smtp_admin: SMTPAdmin):
    # Given an organization without any usable transport, when read through HTTP.
    async with AsyncClient(transport=ASGITransport(app=smtp_admin.app), base_url="http://test") as client:
        result = await client.get(BASE)
    # Then safe defaults are complete and secrets are absent.
    assert result.json() == {
        "enabled": False,
        "from_address": "",
        "from_name": "",
        "smtp_host": "",
        "smtp_port": 465,
        "smtp_username": "",
        "smtp_ssl": True,
        "smtp_timeout_seconds": 15,
        "has_password": False,
        "effective_source": "none",
    }


@pytest.mark.parametrize("system_slug", ["maraclaw", "openclaw"])
@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "POST"])
async def test_system_org_boundary(smtp_admin: SMTPAdmin, system_slug: str, method: str, monkeypatch):
    # Given the own genesis admin in either system organization.
    row = next(row for row in smtp_admin.tenants.values() if row.slug == system_slug)
    smtp_admin.actor.tenant_id = row.id
    smtp_admin.saved[row.id] = email_config.stored_settings(
        TenantEmailUpdate.model_validate({**COMPLETE, "password": "credential"}), {}, public_only=True
    )
    send = Mock()
    monkeypatch.setattr(system_email_service, "_send_email_with_config_sync", send)
    # When the OA reads, edits, deletes or tests their own settings.
    async with AsyncClient(transport=ASGITransport(app=smtp_admin.app), base_url="http://test") as client:
        result = await client.request(
            method, BASE + ("/test" if method == "POST" else ""), json={} if method == "PUT" else None
        )
    # Then neither system slug grants or removes membership-scoped privileges.
    assert result.status_code == (204 if method == "DELETE" else 200)
    assert "credential" not in result.text


@pytest.mark.parametrize("password", [None, "", "replacement"])
async def test_put_preserves_or_replaces_encrypted_password(smtp_admin: SMTPAdmin, password: str | None):
    # Given a saved secret and an omitted/null/blank/new password update.
    tenant_id = smtp_admin.tenant_id
    smtp_admin.saved[tenant_id] = email_config.stored_settings(
        TenantEmailUpdate.model_validate({**COMPLETE, "password": "original"}), {}, public_only=True
    )
    payload = {**COMPLETE, "password": password}
    # When the organization saves via HTTP.
    async with AsyncClient(transport=ASGITransport(app=smtp_admin.app), base_url="http://test") as client:
        result = await client.put(BASE, json=payload)
    # Then the credential remains encrypted and absent from response and audit.
    assert result.status_code == 200
    assert result.json()["has_password"] is True
    assert result.json()["effective_source"] == "tenant"
    assert email_config.load_settings(smtp_admin.saved[tenant_id]).password == (password or "original")
    assert smtp_admin.saved[tenant_id]["password"] not in ("original", "replacement")
    assert "password" not in result.json()
    assert "original" not in repr(smtp_admin.audits)
    assert "replacement" not in repr(smtp_admin.audits)


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "POST"])
@pytest.mark.parametrize("change", [{"role": "member"}, {"is_genesis": False}, {"tenant_id": uuid4()}])
async def test_all_actions_recheck_fresh_actor(smtp_admin: SMTPAdmin, method: str, change):
    # Given a principal whose authorization changed after authentication.
    smtp_admin.fresh = replace(smtp_admin.actor, **change)
    # When any operation reloads the principal.
    async with AsyncClient(transport=ASGITransport(app=smtp_admin.app), base_url="http://test") as client:
        result = await client.request(
            method, BASE + ("/test" if method == "POST" else ""), json={} if method == "PUT" else None
        )
    # Then stale privilege cannot read, write or test.
    assert result.status_code == 403
    assert smtp_admin.audits == []


async def test_own_genesis_pa_defaults_to_own_membership(smtp_admin: SMTPAdmin):
    # Given a genesis platform administrator without an explicit target.
    smtp_admin.actor.role = UserRole.PLATFORM_ADMIN
    # When their isolated SMTP settings are requested.
    async with AsyncClient(transport=ASGITransport(app=smtp_admin.app), base_url="http://test") as client:
        result = await client.get(BASE)
    # Then own-membership defaults apply to PA exactly as to OA.
    assert result.status_code == 200
    assert result.json()["effective_source"] == "none"


async def test_test_uses_disabled_saved_config_and_current_admin_only(smtp_admin: SMTPAdmin, monkeypatch):
    # Given distinct platform settings and a disabled tenant transport.
    smtp_admin.configure_maraclaw()
    smtp_admin.saved[smtp_admin.tenant_id] = email_config.stored_settings(
        TenantEmailUpdate.model_validate({**COMPLETE, "enabled": False}), {}, public_only=True
    )
    send = Mock()
    monkeypatch.setattr(system_email_service, "_send_email_with_config_sync", send)
    # When an arbitrary recipient/body is supplied to the bodyless test endpoint.
    async with AsyncClient(transport=ASGITransport(app=smtp_admin.app), base_url="http://test") as client:
        result = await client.post(
            BASE + "/test", json={"email": "victim@example.com", "smtp_host": "evil.example.com"}
        )
    # Then only the saved tenant host and current admin recipient are used.
    assert result.status_code == 200
    assert send.call_args.args[0].smtp_host == "smtp.example.com"
    assert send.call_args.args[1] == "admin@example.com"
    assert send.call_args.args[0].public_only is True


async def test_test_never_falls_back_to_platform(smtp_admin: SMTPAdmin, monkeypatch):
    # Given working platform SMTP but no saved tenant transport.
    smtp_admin.configure_maraclaw()
    send = Mock()
    monkeypatch.setattr(system_email_service, "_send_email_with_config_sync", send)
    # When test is requested, then return an actionable error without platform delivery.
    async with AsyncClient(transport=ASGITransport(app=smtp_admin.app), base_url="http://test") as client:
        result = await client.post(BASE + "/test")
    assert result.status_code == 400
    send.assert_not_called()


@pytest.mark.parametrize("method", ["PUT", "POST"])
async def test_non_pa_host_guard_cannot_use_pa_private_relay(smtp_admin: SMTPAdmin, method: str, monkeypatch):
    # Given a private relay previously saved by a platform administrator.
    from app.core.outbound_policy import OutboundPolicyError

    smtp_admin.saved[smtp_admin.tenant_id] = email_config.stored_settings(
        TenantEmailUpdate.model_validate(COMPLETE), {}, public_only=False
    )
    monkeypatch.setattr(
        tenant_email_admin, "resolve_public_addresses", Mock(side_effect=OutboundPolicyError("private"))
    )
    # When an OA edits or tests that config, then public-host authorization is reapplied.
    async with AsyncClient(transport=ASGITransport(app=smtp_admin.app), base_url="http://test") as client:
        result = await client.request(
            method, BASE + ("/test" if method == "POST" else ""), json=COMPLETE if method == "PUT" else None
        )
    assert result.status_code == 422
    assert smtp_admin.audits == []
