import re
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from tests.tenant_email_fixtures import SMTPAdmin, smtp_admin

__all__ = ["smtp_admin"]

from app.api import enterprise
from app.api.enterprise import InvitationCodeCreate
from app.records.invitation import InvitationCodeRecord
from app.schemas.schemas import UserInviteRequest
from app.schemas.tenant_email import TenantEmailUpdate
from app.services import email_config, system_email_service
from app.services.invitation_codes import invitation_status, normalize_invite_email

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


async def test_issued_email_link_preserves_invitation_in_transfer_fragment(smtp_admin: SMTPAdmin, monkeypatch):
    # Given a tenant transport and the real invitation HTTP route.
    smtp_admin.saved[smtp_admin.tenant_id] = email_config.stored_settings(
        TenantEmailUpdate(enabled=True, smtp_host="tenant.example.com", from_address="tenant@example.com"),
        {},
        public_only=True,
    )
    deliver = AsyncMock()
    monkeypatch.setattr(system_email_service, "send_company_invitation_email", deliver)
    monkeypatch.setattr(enterprise, "optional_connection_ctx", lambda: nullcontext())
    monkeypatch.setattr(enterprise, "run_after_commit", lambda callback: callback())
    monkeypatch.setattr(enterprise.invitation_code_dao, "create", AsyncMock())
    monkeypatch.setattr(
        enterprise.platform_service, "get_public_base_url", AsyncMock(return_value="https://app.example.com")
    )

    # When the admin issues an emailed invitation.
    async with AsyncClient(transport=ASGITransport(app=smtp_admin.app), base_url="http://test") as client:
        response = await client.post("/api/enterprise/invite-users", json={"emails": ["invitee@example.com"]})

    # Then opening the URL uses the code-aware transfer flow without leaking the code in a query.
    assert response.status_code == 200
    assert deliver.await_args is not None
    link = urlsplit(deliver.await_args.kwargs["invite_url"])
    assert (link.scheme, link.netloc, link.path, link.query) == ("https", "app.example.com", "/transfer", "")
    assert link.fragment == f"code={deliver.await_args.kwargs['invite_code']}"


@pytest.mark.parametrize("ttl", [0, -1, 337])
def test_ttl_outside_one_to_336_hours_is_rejected(ttl: int):
    with pytest.raises(ValidationError):
        _ = InvitationCodeCreate(email="a@example.com", ttl_hours=ttl)
    with pytest.raises(ValidationError):
        _ = UserInviteRequest(emails=["a@example.com"], ttl_hours=ttl)


def test_ttl_defaults_to_the_14_day_maximum():
    assert InvitationCodeCreate(email="a@example.com").ttl_hours == 336
    assert UserInviteRequest(emails=["a@example.com"]).ttl_hours == 336


@pytest.mark.parametrize("email", [None, "", "   ", "nope"])
def test_generated_codes_need_a_valid_recipient(email: str | None):
    with pytest.raises(ValidationError):
        _ = InvitationCodeCreate.model_validate({"email": email})
    with pytest.raises(ValidationError):
        _ = InvitationCodeCreate.model_validate({})


def test_generated_codes_are_single_use():
    with pytest.raises(ValidationError):
        _ = InvitationCodeCreate(email="a@example.com", max_uses=2)


@pytest.mark.parametrize("emails", [[""], ["nope"]])
def test_emailed_invitations_need_valid_recipients(emails: list[str]):
    with pytest.raises(ValidationError):
        _ = UserInviteRequest(emails=emails)


def test_recipient_normalization_is_case_and_whitespace_insensitive():
    assert normalize_invite_email("  New.Hire@Example.COM ") == "new.hire@example.com"
    assert normalize_invite_email("   ") is None


@pytest.mark.parametrize(
    ("fields", "status"),
    [
        ({}, "active"),
        ({"used_count": 1}, "used"),
        ({"is_active": False}, "revoked"),
        ({"is_active": False, "used_count": 1}, "revoked"),
        ({"expires_at": NOW - timedelta(seconds=1)}, "expired"),
        ({"expires_at": NOW}, "expired"),
        ({"used_count": 1, "expires_at": NOW - timedelta(days=1)}, "used"),
    ],
)
def test_status_is_derived_from_lifecycle_state(fields: dict[str, object], status: str):
    base = InvitationCodeRecord(
        id=uuid4(), code="c", expires_at=NOW + timedelta(hours=1), invited_email="a@example.com"
    )
    invitation = replace(base, **fields)
    assert invitation_status(invitation, NOW) == status


def test_invitation_template_variables_are_all_declared():
    template = system_email_service.DEFAULT_EMAIL_TEMPLATES["company_invitation"]
    used = set(re.findall(r"{{(\w+)}}", template["subject"] + template["body"]))
    assert used <= set(system_email_service.EMAIL_TEMPLATE_VARIABLES["company_invitation"])
    assert {"invite_code", "expires_at"} <= used


@pytest.mark.parametrize("custom", [False, True])
async def test_invitation_email_always_carries_code_and_expiry(monkeypatch: pytest.MonkeyPatch, custom: bool):
    # Given a custom template that knows nothing about the new variables, or the default template.
    expires = datetime(2026, 11, 7, 9, 30, tzinfo=UTC)
    captured: dict[str, str] = {}

    async def render(key: str, variables: dict[str, str]) -> tuple[str, str]:
        captured.update(variables)
        return ("Subject", "Join us: " + variables["invite_url"] if custom else "Code {{x}}")

    send = AsyncMock()
    monkeypatch.setattr(system_email_service, "render_email_template", render)
    monkeypatch.setattr(system_email_service, "send_system_email", send)

    # When the invitation is delivered.
    await system_email_service.send_company_invitation_email(
        "a@example.com",
        "Admin",
        "Acme",
        "https://app.example.com/login?code=SECRET",
        tenant_id=uuid4(),
        invite_code="SECRET",
        expires_at=expires,
    )

    # Then the template receives the code and expiry, and the sent body states the expiry either way.
    assert (captured["invite_code"], captured["expires_at"]) == ("SECRET", "2026-11-07 09:30 UTC")
    assert send.await_args is not None
    sent_body = send.await_args.args[2]
    assert "2026-11-07 09:30 UTC" in sent_body
