from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.config import get_settings
from app.dao.identity_provider_dao import identity_provider_dao
from app.records.identity import IdentityProviderRecord
from app.records.sso_scan_session import SSOScanSessionRecord
from app.services.sso_scan_authority import ScanLoginAuthority, ScanLoginAuthorityError, capture_scan_authority


def scan() -> SSOScanSessionRecord:
    return SSOScanSessionRecord(uuid4(), expires_at=datetime.now(UTC) + timedelta(minutes=5))


def test_captured_configuration_has_no_mutable_reference_to_provider_or_adapter() -> None:
    provider = IdentityProviderRecord(
        uuid4(), "dingtalk", "DingTalk", sso_login_enabled=True, config={"nested": {"secret": "captured"}}
    )
    authority = ScanLoginAuthority.capture(scan(), provider, "captured-revision")
    provider.config["nested"]["secret"] = "rotated"
    adapter = authority.adapter()
    assert adapter.config == {"nested": {"secret": "captured"}}
    adapter.config["nested"] = {"secret": "adapter-mutated"}
    assert authority.adapter().config == {"nested": {"secret": "captured"}}
    assert authority.matches(provider, "captured-revision") is False


@pytest.mark.parametrize(("active", "sso"), [(False, True), (True, False)])
async def test_revoked_feishu_row_never_uses_settings_bootstrap(
    monkeypatch: pytest.MonkeyPatch, active: bool, sso: bool
) -> None:
    provider = IdentityProviderRecord(uuid4(), "feishu", "Feishu", is_active=active, sso_login_enabled=sso)
    create = AsyncMock()
    monkeypatch.setattr(identity_provider_dao, "get_by_type_and_tenant", AsyncMock(return_value=provider))
    monkeypatch.setattr(identity_provider_dao, "create", create)
    with pytest.raises(ScanLoginAuthorityError):
        await capture_scan_authority("feishu", scan())
    create.assert_not_awaited()


async def test_feishu_settings_bootstrap_is_captured_before_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "FEISHU_APP_ID", "settings-app")
    monkeypatch.setattr(settings, "FEISHU_APP_SECRET", "settings-secret")
    provider = IdentityProviderRecord(
        uuid4(),
        "feishu",
        "Feishu",
        sso_login_enabled=True,
        config={"app_id": "settings-app", "app_secret": "settings-secret"},
    )
    create = AsyncMock(return_value=provider)
    monkeypatch.setattr(identity_provider_dao, "get_by_type_and_tenant", AsyncMock(return_value=None))
    monkeypatch.setattr(identity_provider_dao, "create", create)
    monkeypatch.setattr(identity_provider_dao, "get_revision", AsyncMock(return_value="captured-revision"))

    authority = await capture_scan_authority("feishu", scan())

    assert authority.provider_id == provider.id
    assert authority.adapter().config == provider.config
    assert create.await_count == 1
    assert create.call_args.kwargs["obj_in"]["sso_login_enabled"] is True


async def test_existing_feishu_empty_config_captures_settings_once(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "FEISHU_APP_ID", "settings-app")
    monkeypatch.setattr(settings, "FEISHU_APP_SECRET", "captured-settings-secret")
    provider = IdentityProviderRecord(uuid4(), "feishu", "Feishu", sso_login_enabled=True)
    monkeypatch.setattr(identity_provider_dao, "get_by_type_and_tenant", AsyncMock(return_value=provider))
    monkeypatch.setattr(identity_provider_dao, "get_revision", AsyncMock(return_value="captured-revision"))
    authority = await capture_scan_authority("feishu", scan())
    monkeypatch.setattr(settings, "FEISHU_APP_SECRET", "rotated-settings-secret")
    assert authority.adapter().config["app_secret"] == "captured-settings-secret"
    assert authority.matches(provider, "captured-revision") is False


def test_restored_provider_values_with_new_revision_reject_captured_authority() -> None:
    provider = IdentityProviderRecord(uuid4(), "wecom", "WeCom", sso_login_enabled=True)
    authority = ScanLoginAuthority.capture(scan(), provider, "initial-revision")
    assert authority.matches(provider, "changed-revision") is False
