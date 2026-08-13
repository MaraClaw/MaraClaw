import pytest
from fastapi import HTTPException

from app.api import enterprise as enterprise_api
from app.services.system_email_service import SystemEmailConfig


def test_new_invitation_code_uses_expected_public_format():
    # Given / When: generating an invitation code
    code = enterprise_api._new_invitation_code()

    # Then: the established eight-character uppercase/digit format is preserved
    assert len(code) == 8
    assert set(code) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


@pytest.mark.asyncio
async def test_invitation_email_preflight_rejects_disabled_system_email(monkeypatch):
    calls = []

    async def fake_resolve_email_config_async(_db, *, include_disabled: bool = False):
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
        await enterprise_api._ensure_invitation_email_enabled(None)

    assert excinfo.value.status_code == 400
    assert "disabled" in excinfo.value.detail
    assert calls == [False, True]


@pytest.mark.asyncio
async def test_invitation_email_preflight_accepts_enabled_system_email(monkeypatch):
    async def fake_resolve_email_config_async(_db, *, include_disabled: bool = False):
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

    await enterprise_api._ensure_invitation_email_enabled(None)
