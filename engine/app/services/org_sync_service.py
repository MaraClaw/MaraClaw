"""Organization structure sync service (provider-based only)."""

from app.core.json_types import JsonObject
from app.core.logging import logger
from app.dao.identity_provider_dao import identity_provider_dao


class OrgSyncService:
    """Sync org structure from a specific identity provider."""

    async def sync_provider(self, provider_id: str) -> JsonObject:
        import uuid as _uuid

        pid = _uuid.UUID(provider_id) if isinstance(provider_id, str) else provider_id

        provider = await identity_provider_dao.get(pid)
        if not provider:
            return {"error": f"Identity provider {provider_id} not found"}

        from app.services.org_sync import get_org_sync_adapter

        adapter = await get_org_sync_adapter(provider.provider_type, provider_id=pid)
        if not adapter:
            return {"error": f"Provider type '{provider.provider_type}' not supported for org sync"}

        # Configure adapter
        adapter.provider = provider
        adapter.config = provider.config

        if not provider.tenant_id:
            return {"error": "Identity provider must be bound to a tenant"}

        adapter.tenant_id = provider.tenant_id

        try:
            return await adapter.sync_org_structure()
        except Exception as e:
            logger.error(f"[OrgSync] Provider sync failed: {e}")
            return {"error": str(e)}


org_sync_service = OrgSyncService()
