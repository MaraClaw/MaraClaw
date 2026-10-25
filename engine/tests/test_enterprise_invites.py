import string
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import enterprise as enterprise_api
from app.services.system_email_service import SystemEmailConfig


def test_new_invitation_code_is_unguessable_and_fits_the_column():
    # Given / When: generating many invitation codes
    codes = {enterprise_api._new_invitation_code() for _ in range(256)}

    # Then: they are distinct URL-safe tokens carrying at least 128 bits within the 32-character column
    assert len(codes) == 256
    assert all(
        22 <= len(code) <= 32 and set(code) <= set(string.ascii_letters + string.digits + "-_") for code in codes
    )


@pytest.mark.asyncio
async def test_invitation_email_preflight_rejects_disabled_system_email(monkeypatch):
    calls = []
    tenant_id = uuid4()

    async def fake_resolve_email_config_async(selected, *, include_disabled: bool = False):
        assert selected == tenant_id
        calls.append(include_disabled)
        if include_disabled:
            return SystemEmailConfig(
                from_address="bot@example.com",
                from_name="MaraClaw",
                smtp_host="smtp.example.com",
                smtp_port=465,
                smtp_username="bot@example.com",
                smtp_password="secret",
                smtp_ssl=True,
                smtp_timeout_seconds=15,
            )
        return None

    monkeypatch.setattr(
        "app.services.system_email_service.resolve_email_config_async",
        fake_resolve_email_config_async,
    )

    with pytest.raises(HTTPException) as excinfo:
        await enterprise_api._ensure_invitation_email_enabled(tenant_id)

    assert excinfo.value.status_code == 400
    assert "disabled" in excinfo.value.detail
    assert calls == [False, True]


@pytest.mark.asyncio
async def test_invitation_email_preflight_accepts_enabled_system_email(monkeypatch):
    tenant_id = uuid4()

    async def fake_resolve_email_config_async(selected, *, include_disabled: bool = False):
        assert selected == tenant_id
        return SystemEmailConfig(
            from_address="bot@example.com",
            from_name="MaraClaw",
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_username="bot@example.com",
            smtp_password="secret",
            smtp_ssl=True,
            smtp_timeout_seconds=15,
        )

    monkeypatch.setattr(
        "app.services.system_email_service.resolve_email_config_async",
        fake_resolve_email_config_async,
    )

    await enterprise_api._ensure_invitation_email_enabled(tenant_id)
