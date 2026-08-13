"""Organization sync adapters and shared helpers."""

from app.services.org_sync.base import BaseOrgSyncAdapter
from app.services.org_sync.dingtalk import DingTalkOrgSyncAdapter
from app.services.org_sync.factory import SYNC_ADAPTER_CLASSES, get_org_sync_adapter
from app.services.org_sync.feishu import FeishuOrgSyncAdapter
from app.services.org_sync.google_workspace import GoogleWorkspaceOrgSyncAdapter
from app.services.org_sync.paths import build_department_path_map, derive_member_department_paths
from app.services.org_sync.types import ExternalDepartment, ExternalUser
from app.services.org_sync.wecom import WeComOrgSyncAdapter

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
