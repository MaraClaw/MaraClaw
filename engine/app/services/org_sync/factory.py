"""Organization sync adapter factory."""

from __future__ import annotations

import uuid

from app.dao import identity_provider_dao

from .base import BaseOrgSyncAdapter
from .dingtalk import DingTalkOrgSyncAdapter
from .feishu import FeishuOrgSyncAdapter
from .google_workspace import GoogleWorkspaceOrgSyncAdapter
from .wecom import WeComOrgSyncAdapter

# Adapter class mapping
SYNC_ADAPTER_CLASSES: dict[str, type[BaseOrgSyncAdapter]] = {
    "feishu": FeishuOrgSyncAdapter,
    "dingtalk": DingTalkOrgSyncAdapter,
    "wecom": WeComOrgSyncAdapter,
    "google_workspace": GoogleWorkspaceOrgSyncAdapter,
}


async def get_org_sync_adapter(
    db: object | None,
    provider_type: str,
    tenant_id: uuid.UUID | None = None,
    provider_id: uuid.UUID | None = None,
) -> BaseOrgSyncAdapter | None:
    """Factory function to create org sync adapter.

    Args:
        db: Database session (accepted for compatibility; ignored - pure-psycopg path)
        provider_type: Type of provider (feishu, dingtalk, etc.)
        tenant_id: Optional tenant ID
        provider_id: Optional specific provider ID (if not provided, uses first found by type)

    Returns:
        Adapter instance or None if not supported
    """
    del db
    if provider_id:
        provider = await identity_provider_dao.get(provider_id)
    else:
        provider = await identity_provider_dao.get_by_type_and_tenant(provider_type, tenant_id)

    adapter_class = SYNC_ADAPTER_CLASSES.get(provider_type)
    if not adapter_class:
        return None

    config = provider.config if provider else {}
    return adapter_class(provider=provider, config=config, tenant_id=tenant_id)
