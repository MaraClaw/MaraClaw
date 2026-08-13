"""Authentication provider registry and factory.

This module provides a centralized way to manage and instantiate auth providers.
"""

from app.core.json_types import JsonObject
from app.dao import identity_provider_dao
from app.records.identity import IdentityProviderRecord
from app.services.auth_provider import (
    PROVIDER_CLASSES,
    BaseAuthProvider,
)


class AuthProviderRegistry:
    """Registry for managing authentication provider instances.

    This class provides a factory method to create provider instances
    and caches them for reuse.
    """

    def __init__(self):
        self._cache: dict[str, BaseAuthProvider] = {}

    async def get_provider(self, provider_type: str, tenant_id: str | None = None) -> BaseAuthProvider | None:
        """Get or create an authentication provider instance."""
        cache_key = f"{provider_type}:{tenant_id or 'global'}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        provider_model = await identity_provider_dao.get_preferred(
            provider_type,
            tenant_id,
            is_active=True,
        )

        provider = self._create_provider(provider_type, provider_model)
        if provider:
            self._cache[cache_key] = provider

        return provider

    def _create_provider(
        self, provider_type: str, provider_model: IdentityProviderRecord | None
    ) -> BaseAuthProvider | None:
        """Create a provider instance based on type."""
        provider_class = PROVIDER_CLASSES.get(provider_type)
        if not provider_class:
            return None

        config = provider_model.config if provider_model else {}
        return provider_class(provider=provider_model, config=config)

    async def list_providers(self, tenant_id: str | None = None) -> list[IdentityProviderRecord]:
        """List all available identity providers."""
        return list(await identity_provider_dao.list_active(tenant_id))

    async def create_provider(
        self,
        db: object | None,
        provider_type: str,
        name: str,
        config: JsonObject,
        tenant_id: str | None = None,
    ) -> IdentityProviderRecord:
        """Create a new identity provider.

        ``db`` is accepted for call-site compatibility and ignored; the DAO
        manages its own psycopg connection/transaction.
        """
        del db
        provider = await identity_provider_dao.create(
            obj_in={
                "provider_type": provider_type,
                "name": name,
                "is_active": True,
                "config": config,
                "tenant_id": tenant_id,
            }
        )
        self._clear_cache(provider_type)
        return provider

    async def update_provider(
        self,
        db: object | None,
        provider_id: str,
        name: str | None = None,
        config: JsonObject | None = None,
        is_active: bool | None = None,
    ) -> IdentityProviderRecord | None:
        """Update an existing identity provider."""
        del db
        provider = await identity_provider_dao.get(provider_id)
        if not provider:
            return None

        updates: JsonObject = {}
        if name is not None:
            updates["name"] = name
        if config is not None:
            updates["config"] = config
        if is_active is not None:
            updates["is_active"] = is_active

        if updates:
            provider = await identity_provider_dao.update(db_obj=provider, obj_in=updates)

        self._clear_cache(provider.provider_type)
        return provider

    async def delete_provider(self, db: object | None, provider_id: str) -> bool:
        """Delete an identity provider."""
        del db
        provider = await identity_provider_dao.get(provider_id)
        if not provider:
            return False

        provider_type = provider.provider_type
        await identity_provider_dao.delete(id=provider_id)
        self._clear_cache(provider_type)
        return True

    def _clear_cache(self, provider_type: str):
        """Clear cached provider instances for a type."""
        keys_to_delete = [k for k in self._cache if k.startswith(f"{provider_type}:")]
        for key in keys_to_delete:
            del self._cache[key]

    def clear_all_cache(self):
        """Clear all cached provider instances."""
        self._cache.clear()


# Global registry instance
auth_provider_registry = AuthProviderRegistry()
