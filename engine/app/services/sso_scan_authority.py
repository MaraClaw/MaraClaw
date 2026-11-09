"""Immutable provider authority captured before scan-login network work."""

import json
from dataclasses import dataclass
from uuid import UUID

from app.config import get_settings
from app.core.json_types import json_loads_object
from app.dao.identity_provider_dao import identity_provider_dao
from app.records.identity import IdentityProviderRecord
from app.records.sso_scan_session import SSOScanSessionRecord
from app.services.auth_provider import PROVIDER_CLASSES, BaseAuthProvider


class ScanLoginAuthorityError(Exception):
    """Captured scan-login authority is unavailable or has changed."""


def _feishu_settings_config_json() -> str:
    settings = get_settings()
    return json.dumps({"app_id": settings.FEISHU_APP_ID, "app_secret": settings.FEISHU_APP_SECRET}, sort_keys=True)


@dataclass(frozen=True, slots=True)
class ScanLoginAuthority:
    session_id: UUID
    session_tenant_id: UUID | None
    provider_id: UUID
    provider_type: str
    tenant_id: UUID | None
    config_json: str
    is_active: bool
    sso_login_enabled: bool
    revision: str
    settings_config_json: str | None = None

    @classmethod
    def capture(
        cls, session: SSOScanSessionRecord, provider: IdentityProviderRecord, revision: str
    ) -> ScanLoginAuthority:
        settings_config_json = None
        if provider.provider_type == "feishu" and not provider.config:
            settings_config_json = _feishu_settings_config_json()
        return cls(
            session.id,
            session.tenant_id,
            provider.id,
            provider.provider_type,
            provider.tenant_id,
            json.dumps(provider.config, sort_keys=True),
            provider.is_active,
            provider.sso_login_enabled,
            revision,
            settings_config_json,
        )

    def matches(self, provider: IdentityProviderRecord, revision: str | None) -> bool:
        return (
            provider.id == self.provider_id
            and provider.provider_type == self.provider_type
            and provider.tenant_id == self.tenant_id
            and provider.is_active == self.is_active
            and provider.sso_login_enabled == self.sso_login_enabled
            and revision == self.revision
            and json.dumps(provider.config, sort_keys=True) == self.config_json
            and (self.settings_config_json is None or self.settings_config_json == _feishu_settings_config_json())
        )

    def adapter(self) -> BaseAuthProvider:
        """Build an uncached adapter with no mutable reference to captured configuration."""
        provider = IdentityProviderRecord(
            self.provider_id,
            self.provider_type,
            self.provider_type,
            is_active=self.is_active,
            sso_login_enabled=self.sso_login_enabled,
            tenant_id=self.tenant_id,
            config=json_loads_object(self.settings_config_json or self.config_json),
        )
        return PROVIDER_CLASSES[self.provider_type](provider=provider)


async def capture_scan_authority(provider_type: str, session: SSOScanSessionRecord) -> ScanLoginAuthority:
    # Include inactive rows: a revoked scoped row must not become a global/settings fallback.
    provider = await identity_provider_dao.get_by_type_and_tenant(provider_type, session.tenant_id)
    if provider is None and session.tenant_id is not None:
        provider = await identity_provider_dao.get_by_type_and_tenant(provider_type, None)
    if provider is None and provider_type == "feishu":
        settings = get_settings()
        if not settings.FEISHU_APP_ID or not settings.FEISHU_APP_SECRET:
            raise ScanLoginAuthorityError
        provider = await identity_provider_dao.create(
            obj_in={
                "provider_type": "feishu",
                "name": "Feishu",
                "tenant_id": session.tenant_id,
                "is_active": True,
                "sso_login_enabled": True,
                "config": {"app_id": settings.FEISHU_APP_ID, "app_secret": settings.FEISHU_APP_SECRET},
            }
        )
    if provider is None or not provider.is_active or not provider.sso_login_enabled:
        raise ScanLoginAuthorityError
    if provider.provider_type != provider_type or (
        provider.tenant_id is not None and provider.tenant_id != session.tenant_id
    ):
        raise ScanLoginAuthorityError
    revision = await identity_provider_dao.get_revision(provider.id)
    if revision is None:
        raise ScanLoginAuthorityError
    return ScanLoginAuthority.capture(session, provider, revision)
