"""Base organization sync adapter and sync coordinator."""

import uuid
from abc import ABC, abstractmethod
from typing import Any, ClassVar

import httpx

from app.core.json_types import JsonObject
from app.core.logging import logger
from app.db.session import optional_connection_ctx
from app.records.identity import IdentityProviderRecord
from app.services.org_sync.departments import OrgSyncDepartmentMixin
from app.services.org_sync.members import OrgSyncMemberMixin
from app.services.org_sync.types import ExternalDepartment, ExternalUser
from app.services.org_sync.utils import _utcnow


class BaseOrgSyncAdapter(OrgSyncDepartmentMixin, OrgSyncMemberMixin, ABC):
    """Abstract base class for organization sync adapters."""

    provider_type: ClassVar[str] = ""

    def __init__(
        self,
        provider: IdentityProviderRecord | None = None,
        config: JsonObject | None = None,
        tenant_id: uuid.UUID | None = None,
    ):
        """Initialize adapter with provider config.

        Args:
            provider: IdentityProvider model from database
            config: Configuration dict (fallback if no provider record)
            tenant_id: Tenant ID for org sync
        """
        self.provider: IdentityProviderRecord | None = provider
        self.config: JsonObject = config or {}
        self.tenant_id: uuid.UUID | None = tenant_id
        self._client: httpx.AsyncClient | None = None

        if provider and provider.config:
            self.config = provider.config

    def _config_string(self, *keys: str) -> str:
        for key in keys:
            value = self.config.get(key)
            if isinstance(value, str):
                return value
        return ""

    @property
    @abstractmethod
    def api_base_url(self) -> str:
        """Base URL for provider API."""

    @abstractmethod
    async def get_access_token(self) -> str:
        """Get valid access token for API calls."""

    @abstractmethod
    async def fetch_departments(self) -> list[ExternalDepartment]:
        """Fetch all departments from provider.

        Returns:
            List of ExternalDepartment
        """

    @abstractmethod
    async def fetch_users(self, department_external_id: str) -> list[ExternalUser]:
        """Fetch users in a department.

        Args:
            department_external_id: External department ID

        Returns:
            List of ExternalUser
        """

    async def sync_org_structure(self, db: object | None = None) -> dict[str, Any]:
        """Main sync function - syncs departments and members.

        Args:
            db: Accepted for dual-stack compatibility and ignored (DAO path).

        Returns:
            Dict with sync results: {"departments": count, "members": count, "users_created": count, "profiles_synced": count, "errors": []}
        """
        del db
        errors = []
        dept_count = 0
        member_count = 0
        user_count = 0
        profile_count = 0
        sync_start = _utcnow()
        partial_failure = False

        # Ensure provider exists
        provider = await self._ensure_provider(None)

        try:
            # Fetch departments over HTTP first, then persist on one connection.
            departments = await self.fetch_departments()
            async with optional_connection_ctx():
                for dept in departments:
                    try:
                        await self._upsert_department(None, provider, dept)
                        dept_count += 1
                    except Exception as e:
                        partial_failure = True
                        errors.append(f"Department {dept.external_id}: {e!s}")
                        logger.error(f"[OrgSync] Failed to sync department {dept.external_id}: {e}")

                _ = await self._rebuild_department_paths(None, provider.id)

            # Fetch users over HTTP; persist each department's members together.
            for dept in departments:
                try:
                    users = await self.fetch_users(dept.external_id)
                except Exception as e:
                    partial_failure = True
                    logger.error(f"[OrgSync] Failed to fetch users in department {dept.external_id}: {e}")
                    errors.append(f"Fetch users in dept {dept.external_id}: {e!s}")
                    continue

                async with optional_connection_ctx():
                    for user in users:
                        try:
                            stats = await self._upsert_member(None, provider, user, dept.external_id)
                            if stats.get("user_created"):
                                user_count += 1
                            if stats.get("profile_synced"):
                                profile_count += 1
                            member_count += 1
                        except Exception as e:
                            partial_failure = True
                            logger.error(f"[OrgSync] Failed to sync member {user.external_id} ({user.name}): {e}")
                            errors.append(f"Member {user.external_id}: {e!s}")

            async with optional_connection_ctx():
                await self._refresh_member_department_paths(None, provider.id)

                if self.provider:
                    from app.dao.identity_provider_dao import identity_provider_dao

                    config = dict(self.provider.config or {})
                    config["last_synced_at"] = _utcnow().isoformat()
                    self.provider = (
                        await identity_provider_dao.update(db_obj=self.provider, obj_in={"config": config})
                        or self.provider
                    )

                    if partial_failure:
                        logger.warning(
                            f"[OrgSync] Skipping reconcile for provider {provider.id} "
                            + "because this sync had partial failures"
                        )
                        errors.append("Reconcile skipped due to partial sync failures")
                    else:
                        await self._reconcile(None, provider.id, sync_start)

                    await self._update_member_counts(None, provider.id)

        except Exception as e:
            import traceback

            logger.error(f"[OrgSync] Critical error during sync: {e}\n{traceback.format_exc()}")
            errors.append(f"Critical: {e!s}")

        return {
            "departments": dept_count,
            "members": member_count,
            "users_created": user_count,
            "profiles_synced": profile_count,
            "errors": errors,
            "provider": self.provider_type,
            "synced_at": _utcnow().isoformat(),
        }
