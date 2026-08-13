"""Compatibility facade for organization sync adapters."""

from app.services.org_sync import (
    SYNC_ADAPTER_CLASSES,
    BaseOrgSyncAdapter,
    DingTalkOrgSyncAdapter,
    ExternalDepartment,
    ExternalUser,
    FeishuOrgSyncAdapter,
    GoogleWorkspaceOrgSyncAdapter,
    WeComOrgSyncAdapter,
    build_department_path_map,
    derive_member_department_paths,
    get_org_sync_adapter,
)

__all__ = [
    "SYNC_ADAPTER_CLASSES",
    "BaseOrgSyncAdapter",
    "DingTalkOrgSyncAdapter",
    "ExternalDepartment",
    "ExternalUser",
    "FeishuOrgSyncAdapter",
    "GoogleWorkspaceOrgSyncAdapter",
    "WeComOrgSyncAdapter",
    "build_department_path_map",
    "derive_member_department_paths",
    "get_org_sync_adapter",
]
