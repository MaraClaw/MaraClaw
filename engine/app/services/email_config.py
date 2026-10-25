"""Encrypted tenant SMTP selection, with MaraClaw as the identity-mail default."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import ValidationError

from app.core.json_types import JsonObject, json_object_from
from app.dao.tenant_dao import tenant_dao
from app.dao.tenant_email_dao import tenant_email_dao
from app.schemas.tenant_email import TenantEmailSettings, TenantEmailUpdate
from app.services.system_org_seeder import MARACLAW_SLUG
from app.services.tool_config import decrypt_sensitive_fields, encrypt_sensitive_fields

type EmailSource = Literal["tenant", "none"]


@dataclass(frozen=True, slots=True)
class SystemEmailConfig:
    from_address: str
    from_name: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_ssl: bool
    smtp_timeout_seconds: int
    public_only: bool = False


def load_settings(value: JsonObject) -> TenantEmailSettings:
    return TenantEmailSettings.model_validate(decrypt_sensitive_fields(value))


def stored_settings(data: TenantEmailUpdate, previous: JsonObject, *, public_only: bool) -> JsonObject:
    value = json_object_from(data.model_dump(mode="json", exclude={"password"}))
    value["password"] = data.password or previous.get("password") or ""
    value["public_only"] = public_only
    return encrypt_sensitive_fields(value)


def transport(settings: TenantEmailSettings, *, include_disabled: bool = False) -> SystemEmailConfig | None:
    if not (settings.enabled or include_disabled) or not (settings.smtp_host and settings.from_address):
        return None
    return SystemEmailConfig(
        from_address=settings.from_address,
        from_name=settings.from_name,
        smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port,
        smtp_username=settings.smtp_username,
        smtp_password=settings.password,
        smtp_ssl=settings.smtp_ssl,
        smtp_timeout_seconds=settings.smtp_timeout_seconds,
        public_only=settings.public_only,
    )


async def saved_transport(tenant_id: UUID, *, include_disabled: bool = False) -> SystemEmailConfig | None:
    value = await tenant_email_dao.get(tenant_id)
    try:
        return transport(load_settings(value), include_disabled=include_disabled)
    except ValidationError, ValueError:
        return None


async def resolve_with_source(
    tenant_id: UUID, *, include_disabled: bool = False
) -> tuple[SystemEmailConfig | None, EmailSource]:
    configured = await saved_transport(tenant_id, include_disabled=include_disabled)
    return configured, "tenant" if configured is not None else "none"


async def resolve_email_config_async(
    tenant_id: UUID | None = None, *, include_disabled: bool = False
) -> SystemEmailConfig | None:
    if tenant_id is None:
        tenant = await tenant_dao.get_by_slug(MARACLAW_SLUG)
        if tenant is None or not tenant.is_system:
            return None
        return await saved_transport(tenant.id)
    return await saved_transport(tenant_id, include_disabled=include_disabled)
