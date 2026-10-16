"""Organization structure sync service (provider-based only)."""

from copy import deepcopy
from typing import cast
from uuid import UUID

from fastapi import HTTPException

from app.core.json_types import JsonObject
from app.core.logging import logger
from app.dao.identity_provider_dao import identity_provider_dao
from app.db.session import get_connection
from app.records.user import UserRecord
from app.services.enterprise_llm import is_platform_admin
from app.services.subscription_boundaries import active_subscription_tenant, fresh_subscription_actor


class OrgSyncService:
    """Sync org structure from a specific identity provider."""

    async def sync_provider(self, provider_id: UUID | str, actor: UserRecord | None = None) -> JsonObject:
        import uuid as _uuid

        pid = _uuid.UUID(provider_id) if isinstance(provider_id, str) else provider_id
        actor_snapshot = deepcopy(actor)

        provider = await identity_provider_dao.get(pid)
        if not provider:
            return {"error": f"Identity provider {provider_id} not found"}
        snapshot = deepcopy(provider)

        from app.services.org_sync import SYNC_ADAPTER_CLASSES

        adapter_class = SYNC_ADAPTER_CLASSES.get(snapshot.provider_type)
        if adapter_class is None:
            return {"error": f"Provider type '{provider.provider_type}' not supported for org sync"}

        if not provider.tenant_id:
            return {"error": "Identity provider must be bound to a tenant"}

        adapter = adapter_class(
            provider=deepcopy(snapshot), config=deepcopy(snapshot.config), tenant_id=snapshot.tenant_id
        )

        async def before_write() -> None:
            fresh_actor = await fresh_subscription_actor(actor_snapshot) if actor_snapshot is not None else None
            if snapshot.tenant_id is not None:
                await active_subscription_tenant(snapshot.tenant_id)
            db = get_connection()
            if db is not None:
                await db.execute("SELECT id FROM identity_providers WHERE id = %(id)s FOR UPDATE", {"id": pid})
            current = await identity_provider_dao.get(pid)
            if current is None:
                raise HTTPException(status_code=404, detail="Provider not found")
            if not current.is_active:
                raise HTTPException(status_code=403, detail="Provider is inactive")
            if (
                current.tenant_id != snapshot.tenant_id
                or current.provider_type != snapshot.provider_type
                or current.config != snapshot.config
            ):
                raise HTTPException(status_code=409, detail="Provider changed during org sync; retry")
            if (
                fresh_actor is not None
                and not is_platform_admin(fresh_actor)
                and current.tenant_id != fresh_actor.tenant_id
            ):
                raise HTTPException(status_code=403, detail="Cannot sync other tenant's provider")
            adapter.provider = current

        try:
            return cast(JsonObject, await adapter.sync_org_structure(before_write))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[OrgSync] Provider sync failed: {e}")
            return {"error": str(e)}


org_sync_service = OrgSyncService()
