"""Short locked SMTP administration transactions, separated from network delivery."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import partial
from uuid import UUID

import anyio
from fastapi import HTTPException

from app.core.acl_locks import lock_tenants, lock_user
from app.core.json_types import json_object_from
from app.core.outbound_policy import OutboundPolicyError, resolve_public_addresses
from app.core.security import raise_if_password_change_required
from app.dao.tenant_dao import tenant_dao
from app.dao.tenant_email_dao import tenant_email_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.schemas.tenant_email import TenantEmailOut, TenantEmailSettings, TenantEmailUpdate
from app.services.admin_audit import field_change, write_admin_audit
from app.services.email_config import load_settings, resolve_with_source, stored_settings, transport


def own_tenant(actor: UserRecord, selected: UUID | None) -> UUID:
    raise_if_password_change_required(actor)
    if actor.role not in ("org_admin", "platform_admin") or not actor.is_genesis or actor.tenant_id is None:
        raise HTTPException(403, "Organization genesis administrator required")
    if selected is not None and selected != actor.tenant_id:
        raise HTTPException(403, "SMTP configuration belongs only to your own organization")
    return actor.tenant_id


async def fresh_actor(actor: UserRecord) -> UserRecord:
    fresh = await user_dao.get_with_identity(actor.id, fresh=True)
    if fresh is None or not fresh.is_active or (fresh.identity is not None and not fresh.identity.is_active):
        raise HTTPException(403, "Administrator is inactive")
    raise_if_password_change_required(fresh)
    return fresh


@asynccontextmanager
async def locked_actor(actor: UserRecord, tenant_id: UUID) -> AsyncGenerator[tuple[UserRecord, TenantRecord]]:
    async with connection_ctx() as db:
        await lock_tenants(db, (tenant_id, actor.tenant_id))
        await lock_user(db, actor.id, actor.tenant_id)
        fresh = await fresh_actor(actor)
        own_tenant(fresh, tenant_id)
        tenant = await tenant_dao.get(tenant_id)
        if tenant is None:
            raise HTTPException(404, "Organization not found")
        yield fresh, tenant


def redacted(settings: TenantEmailSettings) -> TenantEmailOut:
    return TenantEmailOut.model_validate(
        {**settings.model_dump(exclude={"password", "public_only"}), "has_password": bool(settings.password)}
    )


async def get_config(actor: UserRecord, selected: UUID | None) -> TenantEmailOut:
    tenant_id = own_tenant(actor, selected)
    own_tenant(await fresh_actor(actor), tenant_id)
    if await tenant_dao.get(tenant_id) is None:
        raise HTTPException(404, "Organization not found")
    settings = load_settings(await tenant_email_dao.get(tenant_id))
    _config, source = await resolve_with_source(tenant_id)
    return redacted(settings).model_copy(update={"effective_source": source})


async def validate_host(settings: TenantEmailSettings) -> None:
    if settings.smtp_host:
        try:
            await resolve_public_addresses(settings.smtp_host, settings.smtp_port)
        except OutboundPolicyError:
            raise HTTPException(422, "SMTP host must resolve only to public network addresses") from None


async def put_config(actor: UserRecord, selected: UUID | None, data: TenantEmailUpdate) -> TenantEmailOut:
    tenant_id = own_tenant(actor, selected)
    async with locked_actor(actor, tenant_id) as (fresh, _tenant):
        before = await tenant_email_dao.get(tenant_id)
        public_only = fresh.role != "platform_admin" or (
            bool(before.get("public_only", True)) and data.smtp_host == before.get("smtp_host")
        )
        value = stored_settings(data, before, public_only=public_only)
        settings = load_settings(value)
        if settings.public_only:
            await validate_host(settings)
        await tenant_email_dao.put(tenant_id, value)
        await write_admin_audit(
            actor=fresh,
            action="tenant_email.update",
            target_type="tenant_email",
            target_id=tenant_id,
            tenant_id=tenant_id,
            changes={
                "configuration": field_change(
                    json_object_from(redacted(load_settings(before)).model_dump()),
                    json_object_from(redacted(settings).model_dump()),
                )
            },
        )
    return await get_config(actor, tenant_id)


async def delete_config(actor: UserRecord, selected: UUID | None) -> None:
    tenant_id = own_tenant(actor, selected)
    async with locked_actor(actor, tenant_id) as (fresh, _tenant):
        await tenant_email_dao.delete(tenant_id)
        await write_admin_audit(
            actor=fresh,
            action="tenant_email.delete",
            target_type="tenant_email",
            target_id=tenant_id,
            tenant_id=tenant_id,
            changes={},
        )


async def test_config(actor: UserRecord, selected: UUID | None) -> None:
    from app.services.system_email_service import _send_email_with_config_sync

    tenant_id = own_tenant(actor, selected)
    async with locked_actor(actor, tenant_id) as (fresh, _tenant):
        settings = load_settings(await tenant_email_dao.get(tenant_id))
        config = transport(settings, include_disabled=True)
        if config is None:
            raise HTTPException(400, "Save the organization's SMTP host and sender before testing")
        recipient = fresh.email
        if recipient is None or recipient == "":
            raise HTTPException(400, "Your administrator account needs an email address")
        if fresh.role != "platform_admin":
            await validate_host(settings)
            config = replace(config, public_only=True)
        await write_admin_audit(
            actor=fresh,
            action="tenant_email.test",
            target_type="tenant_email",
            target_id=tenant_id,
            tenant_id=tenant_id,
            changes={},
        )
    await anyio.to_thread.run_sync(
        partial(
            _send_email_with_config_sync,
            config,
            recipient,
            "MaraClaw SMTP test",
            "Your saved SMTP configuration works.",
        )
    )
