"""Helpers for resolving identity providers safely (psycopg DAO-backed)."""

from __future__ import annotations

from collections.abc import Iterable

from app.core.logging import logger
from app.dao import identity_provider_dao
from app.dao.base import as_uuid
from app.records.identity import IdentityProviderRecord


def choose_preferred_identity_provider(
    providers: Iterable[IdentityProviderRecord],
    *,
    provider_type: str,
    tenant_id: str | None = None,
) -> IdentityProviderRecord | None:
    """Pick the preferred provider and warn when duplicates are present."""
    items = list(providers)
    if not items:
        return None

    if len(items) > 1:
        logger.warning(
            "Multiple identity providers found for type=%s tenant_id=%s; using provider_id=%s",
            provider_type,
            tenant_id,
            items[0].id,
        )
    return items[0]


async def get_preferred_identity_provider(
    provider_type: str,
    tenant_id: str | None = None,
    *,
    is_active: bool | None = True,
    db: object | None = None,
) -> IdentityProviderRecord | None:
    """Fetch the preferred provider without raising on duplicate rows.

    ``db`` is accepted for call-site compatibility and ignored.
    """
    del db
    return await identity_provider_dao.get_preferred(
        str(provider_type),
        as_uuid(tenant_id),
        is_active=is_active,
    )
